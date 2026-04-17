"""Facebook Webhook handler for Lead Ads with full payload tracking."""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse, JSONResponse

from app.config import settings
from app.fb_client import get_lead_data
from app.integrations_store import find_by_fb_page_and_form
from app.lead_tracker import log_lead_event
from app.medidesk_client import submit_form_urlencoded

logger = logging.getLogger(__name__)


def _verify_signature(payload: bytes, signature_header: str | None) -> bool:
    """Verify X-Hub-Signature-256 from Facebook using app_secret."""
    if not settings.fb_app_secret:
        logger.warning("fb_app_secret not set — skipping webhook signature check")
        return True  # allow in dev
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.fb_app_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header[7:])

router = APIRouter(prefix="/webhook", tags=["Webhook"])


@router.get("/facebook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """Facebook webhook verification (challenge-response)."""
    if hub_mode == "subscribe" and hub_verify_token == settings.fb_webhook_verify_token:
        logger.info("Webhook verified successfully")
        return PlainTextResponse(hub_challenge or "")

    logger.warning("Webhook verification failed: mode=%s token=%s", hub_mode, hub_verify_token)
    return PlainTextResponse("Verification failed", status_code=403)


@router.post("/facebook")
async def handle_webhook(request: Request):
    """Handle incoming Facebook webhook events (leadgen)."""
    # Verify webhook signature (HMAC-SHA256)
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not _verify_signature(raw_body, signature):
        logger.warning("Webhook signature verification FAILED")
        return JSONResponse(status_code=403, content={"error": "Invalid signature"})

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    obj = body.get("object")
    if obj != "page":
        logger.info("Webhook received non-page object: %s", obj)
        return JSONResponse(content={"status": "ignored"})

    entries = body.get("entry", [])
    processed = 0

    for entry in entries:
        page_id = entry.get("id", "")
        changes = entry.get("changes", [])

        for change in changes:
            if change.get("field") != "leadgen":
                continue

            value = change.get("value", {})
            lead_id = value.get("leadgen_id")
            form_id = value.get("form_id")

            if not lead_id:
                logger.warning("Webhook leadgen event without leadgen_id")
                continue

            logger.info(
                "New lead received: lead_id=%s form_id=%s page_id=%s",
                lead_id, form_id, page_id,
            )

            # Find the integration for this page + form
            integration = find_by_fb_page_and_form(page_id, form_id)
            if not integration:
                logger.warning("No active integration found for page %s", page_id)
                continue

            # Log: received
            log_lead_event(
                integration_id=integration.id,
                lead_id=lead_id,
                status="received",
                medidesk_form_id=integration.medidesk_form_id,
            )

            # Fetch lead data from Facebook
            lead = await get_lead_data(lead_id, integration.fb_page_token)
            if not lead:
                logger.error("Failed to fetch lead %s from Facebook", lead_id)
                log_lead_event(
                    integration_id=integration.id,
                    lead_id=lead_id,
                    status="failed",
                    error="Failed to fetch lead from Facebook",
                    medidesk_form_id=integration.medidesk_form_id,
                )
                continue

            # Map FB fields to Medidesk fields (supports merge: multiple FB → one MD)
            fields_values: dict[str, str] = {}

            # Sort mappings so first_name comes before last_name (correct merge order)
            sorted_mappings = sorted(
                integration.field_mappings,
                key=lambda m: (m.medidesk_field, m.fb_field),
            )

            for mapping in sorted_mappings:
                fb_key = mapping.fb_field

                # Virtual fields: inject computed values
                if fb_key == "__fb_form_name__":
                    fb_value = integration.fb_form_name
                elif fb_key == "__fb_lead_date__":
                    from datetime import datetime, timezone
                    fb_value = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                else:
                    fb_value = lead.field_data.get(fb_key, "")

                if fb_value:
                    if mapping.medidesk_field in fields_values:
                        # Merge: append with space (e.g., first_name + last_name)
                        fields_values[mapping.medidesk_field] += " " + fb_value
                    else:
                        fields_values[mapping.medidesk_field] = fb_value

            if not fields_values:
                logger.warning("No mapped fields with values for lead %s", lead_id)
                log_lead_event(
                    integration_id=integration.id,
                    lead_id=lead_id,
                    status="failed",
                    mapped_fields_count=0,
                    error="No mapped fields with values",
                    fb_raw_data=lead.field_data,
                    mapped_values={},
                    medidesk_form_id=integration.medidesk_form_id,
                )
                continue

            # Submit to Medidesk
            result = await submit_form_urlencoded(
                integration.medidesk_form_id,
                fields_values,
            )

            if result.success:
                logger.info(
                    "Lead %s successfully sent to Medidesk form %s",
                    lead_id, integration.medidesk_form_id,
                )
                log_lead_event(
                    integration_id=integration.id,
                    lead_id=lead_id,
                    status="sent",
                    mapped_fields_count=len(fields_values),
                    fb_raw_data=lead.field_data,
                    mapped_values=fields_values,
                    medidesk_form_id=integration.medidesk_form_id,
                )
                processed += 1
            else:
                logger.error(
                    "Failed to send lead %s to Medidesk: status=%s",
                    lead_id, result.status_code,
                )
                log_lead_event(
                    integration_id=integration.id,
                    lead_id=lead_id,
                    status="failed",
                    mapped_fields_count=len(fields_values),
                    error=f"Medidesk HTTP {result.status_code}",
                    fb_raw_data=lead.field_data,
                    mapped_values=fields_values,
                    medidesk_form_id=integration.medidesk_form_id,
                )

    return JSONResponse(content={"status": "ok", "processed": processed})
