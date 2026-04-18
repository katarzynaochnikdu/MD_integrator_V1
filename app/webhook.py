"""Facebook Webhook handler for Lead Ads with full payload tracking."""
from __future__ import annotations

import hashlib
import hmac
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse, JSONResponse

from app.config import settings
from app.fb_client import get_lead_data
from app.integrations_store import find_by_fb_page_and_form
from app.lead_tracker import log_lead_event
from app.medidesk_client import submit_form_urlencoded

logger = logging.getLogger(__name__)

RECENT_ATTEMPTS: deque[dict[str, Any]] = deque(maxlen=20)


def build_medidesk_fields(
    integration: Any,
    fb_field_data: dict[str, str],
    lead_meta: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Map FB field data → Medidesk fields using integration mappings + type-aware normalization.

    Shared by the live webhook path and the /retry endpoint so mapping edits take effect.
    """
    lead_meta = lead_meta or {}
    fields_values: dict[str, str] = {}

    sorted_mappings = sorted(
        integration.field_mappings,
        key=lambda m: (m.medidesk_field, m.fb_field),
    )

    for mapping in sorted_mappings:
        # Persisted "skip" marker from the edit UI — user explicitly set this
        # FB field to "— Nie mapuj —". Keep the record (so the choice survives
        # reload) but don't produce any Medidesk value for it.
        if not (mapping.medidesk_field or "").strip():
            continue
        fb_key = mapping.fb_field
        if fb_key.startswith("__const:") and fb_key.endswith("__"):
            fb_value = fb_key[8:-2]
        elif fb_key.startswith("__CONST__"):
            fb_value = fb_key[9:]
        elif fb_key.startswith("__fb_"):
            virtual_map = {
                "__fb_form_name__": getattr(integration, "fb_form_name", "") or "",
                "__fb_lead_date__": lead_meta.get("created_time")
                or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "__fb_ad_name__": lead_meta.get("ad_name") or "",
                "__fb_adset_name__": lead_meta.get("adset_name") or "",
                "__fb_campaign_name__": lead_meta.get("campaign_name") or "",
                "__fb_platform__": lead_meta.get("platform") or "",
                "__fb_is_organic__": "tak" if lead_meta.get("is_organic") else "nie",
                "__fb_lead_id__": lead_meta.get("lead_id") or "",
            }
            fb_value = virtual_map.get(fb_key, "")
        else:
            fb_value = fb_field_data.get(fb_key, "")

        if fb_value:
            if mapping.medidesk_field in fields_values:
                fields_values[mapping.medidesk_field] += " " + fb_value
            else:
                fields_values[mapping.medidesk_field] = fb_value

    md_field_by_id: dict[str, dict[str, Any]] = {
        (f.get("fieldId") or f.get("id") or f.get("name")): f
        for f in (getattr(integration, "medidesk_fields", None) or [])
    }
    for md_id, val in list(fields_values.items()):
        md_meta = md_field_by_id.get(md_id) or {}
        mtype = (md_meta.get("type") or "").lower()
        sval = str(val).strip()
        if mtype in ("checkbox", "boolean", "bool", "consent"):
            if sval.lower() in ("true", "1", "yes", "tak", "on", "y", "t"):
                fields_values[md_id] = "true"
            elif sval.lower() in ("false", "0", "no", "nie", "off", "n", "f", ""):
                fields_values[md_id] = "false"
        elif mtype in ("select", "lista", "dropdown", "radio"):
            options = md_meta.get("options") or []
            if options and sval not in options:
                match = next((o for o in options if str(o).lower() == sval.lower()), None)
                if match is not None:
                    fields_values[md_id] = str(match)

    return fields_values


def _verify_signature(payload: bytes, signature_header: str | None) -> bool:
    """Verify X-Hub-Signature-256 from Facebook using app_secret."""
    if not settings.fb_app_secret:
        logger.critical("fb_app_secret NOT SET — rejecting webhook (fail-closed)")
        return False  # fail-closed: never allow unsigned webhooks
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


def _record_attempt(**fields: Any) -> None:
    """Append a diagnostic record for the most recent webhook attempts."""
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **fields}
    RECENT_ATTEMPTS.append(entry)


@router.post("/facebook")
async def handle_webhook(request: Request):
    """Handle incoming Facebook webhook events (leadgen)."""
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    client_ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")

    logger.info(
        "Webhook POST received: ip=%s bytes=%d has_signature=%s ua=%s",
        client_ip, len(raw_body), bool(signature), user_agent[:80],
    )

    if not _verify_signature(raw_body, signature):
        logger.warning("Webhook signature verification FAILED")
        _record_attempt(
            client_ip=client_ip,
            body_size=len(raw_body),
            has_signature=bool(signature),
            signature_valid=False,
            response_status=403,
            reject_reason="invalid_signature",
        )
        return JSONResponse(status_code=403, content={"error": "Invalid signature"})

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        _record_attempt(
            client_ip=client_ip,
            body_size=len(raw_body),
            has_signature=bool(signature),
            signature_valid=True,
            response_status=400,
            reject_reason="invalid_json",
        )
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    obj = body.get("object")
    if obj != "page":
        logger.info("Webhook received non-page object: %s", obj)
        _record_attempt(
            client_ip=client_ip,
            body_size=len(raw_body),
            has_signature=bool(signature),
            signature_valid=True,
            response_status=200,
            parsed_object=obj,
            reject_reason="non_page_object",
        )
        return JSONResponse(content={"status": "ignored"})

    entries = body.get("entry", [])
    processed = 0
    page_ids_seen: list[str] = []
    integrations_matched = 0
    integrations_missed = 0

    for entry in entries:
        page_id = entry.get("id", "")
        if page_id:
            page_ids_seen.append(page_id)
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
                integrations_missed += 1
                continue
            integrations_matched += 1

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

            fields_values = build_medidesk_fields(
                integration,
                lead.field_data,
                {
                    "created_time": lead.created_time,
                    "ad_name": lead.ad_name,
                    "adset_name": lead.adset_name,
                    "campaign_name": lead.campaign_name,
                    "platform": lead.platform,
                    "is_organic": lead.is_organic,
                    "lead_id": lead.lead_id,
                },
            )

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
                    "Failed to send lead %s to Medidesk: status=%s body=%s",
                    lead_id, result.status_code, (result.raw_text or "")[:500],
                )
                error_msg = f"Medidesk HTTP {result.status_code}"
                if result.raw_text:
                    error_msg += f": {result.raw_text[:800]}"
                log_lead_event(
                    integration_id=integration.id,
                    lead_id=lead_id,
                    status="failed",
                    mapped_fields_count=len(fields_values),
                    error=error_msg,
                    fb_raw_data=lead.field_data,
                    mapped_values=fields_values,
                    medidesk_form_id=integration.medidesk_form_id,
                )

    _record_attempt(
        client_ip=client_ip,
        body_size=len(raw_body),
        has_signature=bool(signature),
        signature_valid=True,
        response_status=200,
        parsed_object=obj,
        page_ids=page_ids_seen,
        integrations_matched=integrations_matched,
        integrations_missed=integrations_missed,
        processed=processed,
    )
    return JSONResponse(content={"status": "ok", "processed": processed})


@router.get("/_debug/attempts")
async def debug_webhook_attempts():
    """Return the last N webhook attempts as recorded by the running process.

    Public but non-sensitive — returns only delivery metadata, not lead payloads.
    """
    return {"count": len(RECENT_ATTEMPTS), "attempts": list(RECENT_ATTEMPTS)}
