from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.config import settings
from app.fb_auth import require_admin, require_auth, router as fb_auth_router
from app.fb_client import subscribe_page_to_webhooks
from app.integrations_store import (
    FieldMapping,
    create_integration,
    delete_integration,
    get_all_integrations,
    get_integration,
    update_integration,
)
from app.mapping_ai import MappingSuggestion, suggest_mapping
from app.medidesk_client import (
    MedideskResult,
    fetch_form_definition,
    submit_form_urlencoded,
)
from app.webhook import router as webhook_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Medidesk Integrator", version="2.0.0")

if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Register routers
app.include_router(fb_auth_router)
app.include_router(webhook_router)


# ─── Existing endpoints ───────────────────────────────────────────────


@app.get("/")
async def root():
    return {
        "service": "Medidesk Integrator",
        "version": "2.0.0",
        "docs": "/docs",
        "usage": "POST /api/submit/{medidesk_form_id} with JSON body of field values",
    }


@app.get("/api/forms/{form_id}/fields")
async def get_form_fields(form_id: str):
    """Podgląd pól formularza Medidesk — pomocne przy tworzeniu mapowania."""
    defn = await fetch_form_definition(form_id)
    if not defn or not defn.fields:
        return JSONResponse(
            status_code=404,
            content={"error": f"Form {form_id} not found or has no fields"},
        )
    return {
        "form_id": form_id,
        "form_name": defn.name,
        "fields": [
            {
                "fieldId": f.field_id,
                "type": f.field_type,
                "required": f.required,
                "name": f.name,
                "options": f.options,
            }
            for f in defn.fields
        ],
    }


@app.post("/api/submit/{form_id}")
async def submit_to_medidesk(form_id: str, request: Request):
    """Generyczny endpoint: przyjmuje JSON z polami, wysyła urlencoded do Medidesk."""
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON body"},
        )

    site_domain = body.pop("siteDomain", None)
    site_url = body.pop("siteUrl", None)

    fields_values: dict[str, str] = {
        k: str(v) for k, v in body.items() if v is not None
    }

    if not fields_values:
        return JSONResponse(
            status_code=400,
            content={"error": "No field values provided"},
        )

    result = await submit_form_urlencoded(
        form_id, fields_values, site_domain, site_url
    )

    if result.success:
        return {"status": "ok", "form_id": form_id}

    if result.status_code == 400 and result.body:
        return JSONResponse(
            status_code=400,
            content={
                "status": "validation_error",
                "form_id": form_id,
                "errors": result.body,
            },
        )

    content: dict[str, Any] = {
        "status": "upstream_error",
        "message": f"Medidesk returned HTTP {result.status_code}",
        "form_id": form_id,
    }
    if settings.debug_upstream:
        content["upstream_body"] = result.body
        content["upstream_preview"] = (result.raw_text or "")[:2000]

    status = result.status_code if result.status_code in (502, 504) else 502
    return JSONResponse(status_code=status, content=content)


# ─── New: Integration management ──────────────────────────────────────


@app.post("/api/mapping/suggest")
async def suggest_field_mapping(request: Request):
    """AI-assisted field mapping suggestions (fuzzy matching)."""
    body = await request.json()
    fb_questions = body.get("fb_questions", [])
    medidesk_fields = body.get("medidesk_fields", [])

    if not fb_questions or not medidesk_fields:
        return JSONResponse(status_code=400, content={"error": "Both fb_questions and medidesk_fields required"})

    suggestions = suggest_mapping(fb_questions, medidesk_fields)
    return {
        "suggestions": [
            {"fb_field": s.fb_field, "medidesk_field": s.medidesk_field, "confidence": s.confidence}
            for s in suggestions
        ],
        "total_fb_fields": len(fb_questions),
        "matched": len(suggestions),
    }


@app.post("/api/integrations")
async def create_new_integration(request: Request, _session=Depends(require_auth)):
    """Create a new FB→Medidesk integration with field mappings."""
    body = await request.json()

    required = ["fb_page_id", "fb_page_name", "fb_page_token",
                 "fb_form_id", "fb_form_name",
                 "medidesk_form_id", "field_mappings"]

    for field in required:
        if field not in body:
            return JSONResponse(status_code=400, content={"error": f"Missing required field: {field}"})

    mappings = [
        FieldMapping(
            fb_field=m["fb_field"],
            medidesk_field=m["medidesk_field"],
            confidence=m.get("confidence", 0.0),
        )
        for m in body["field_mappings"]
    ]

    integration = create_integration(
        fb_page_id=body["fb_page_id"],
        fb_page_name=body["fb_page_name"],
        fb_page_token=body["fb_page_token"],
        fb_form_id=body["fb_form_id"],
        fb_form_name=body["fb_form_name"],
        fb_form_questions=body.get("fb_form_questions", []),
        medidesk_form_id=body["medidesk_form_id"],
        medidesk_form_name=body.get("medidesk_form_name", ""),
        medidesk_fields=body.get("medidesk_fields", []),
        field_mappings=mappings,
    )

    return {"status": "created", "integration": asdict(integration)}


@app.get("/api/integrations")
async def list_integrations(_session=Depends(require_auth)):
    """List all integrations (tokens hidden)."""
    integrations = get_all_integrations()
    return {
        "integrations": [
            {
                "id": i.id,
                "fb_page_id": i.fb_page_id,
                "fb_page_name": i.fb_page_name,
                "fb_form_name": i.fb_form_name,
                "medidesk_form_id": i.medidesk_form_id,
                "medidesk_form_name": i.medidesk_form_name,
                "active": i.active,
                "mappings_count": len(i.field_mappings),
                "created_at": i.created_at,
            }
            for i in integrations
        ]
    }


@app.get("/api/integrations/{integration_id}")
async def get_integration_detail(integration_id: str, _session=Depends(require_auth)):
    """Get details of a specific integration (token hidden)."""
    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})
    data = asdict(integration)
    data.pop("fb_page_token", None)  # Never expose token via API
    return data


@app.post("/api/integrations/{integration_id}/activate")
async def activate_integration(integration_id: str, _session=Depends(require_auth)):
    """Activate an integration and subscribe page to webhooks."""
    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})

    # Subscribe page to webhooks
    success = await subscribe_page_to_webhooks(
        integration.fb_page_id, integration.fb_page_token
    )

    if not success:
        return JSONResponse(
            status_code=502,
            content={"error": "Failed to subscribe page to webhooks. Check FB permissions."},
        )

    updated = update_integration(integration_id, active=True)
    return {"status": "activated", "integration_id": integration_id}


@app.post("/api/integrations/{integration_id}/deactivate")
async def deactivate_integration(integration_id: str, _session=Depends(require_auth)):
    """Deactivate an integration."""
    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})
    update_integration(integration_id, active=False)
    return {"status": "deactivated", "integration_id": integration_id}


@app.delete("/api/integrations/{integration_id}")
async def remove_integration(integration_id: str, _session=Depends(require_admin)):
    """Delete an integration."""
    if delete_integration(integration_id):
        return {"status": "deleted"}
    return JSONResponse(status_code=404, content={"error": "Integration not found"})


# ─── Stats endpoints ──────────────────────────────────────────────


@app.get("/api/stats")
async def global_stats(_session=Depends(require_auth)):
    """Global lead processing stats."""
    from app.lead_tracker import get_global_stats, get_recent_leads
    from app.integrations_store import get_all_integrations

    stats = get_global_stats()
    integrations = get_all_integrations()
    stats["active_integrations"] = sum(1 for i in integrations if i.active)
    stats["total_integrations"] = len(integrations)
    stats["recent_leads"] = get_recent_leads(limit=10)
    return stats


@app.get("/api/stats/{integration_id}")
async def integration_stats(integration_id: str, _session=Depends(require_auth)):
    """Stats for a specific integration."""
    from app.lead_tracker import get_stats, get_recent_leads

    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})

    stats = get_stats(integration_id)
    return {
        "integration_id": integration_id,
        "total": stats.total,
        "sent": stats.sent,
        "failed": stats.failed,
        "success_rate": stats.success_rate,
        "last_lead_at": stats.last_lead_at,
        "recent_leads": get_recent_leads(integration_id, limit=20),
    }


@app.get("/api/leads/failed")
async def list_failed_leads(_session=Depends(require_auth)):
    """Get all failed leads (not yet successfully retried)."""
    from app.lead_tracker import get_failed_leads
    return {"failed_leads": get_failed_leads()}


@app.get("/api/leads/{lead_id}")
async def get_lead_detail(lead_id: str, _session=Depends(require_auth)):
    """Get full details of a lead event (including raw data)."""
    from app.lead_tracker import get_lead_event
    event = get_lead_event(lead_id)
    if not event:
        return JSONResponse(status_code=404, content={"error": "Lead not found"})
    return event


@app.post("/api/leads/{lead_id}/retry")
async def retry_lead(lead_id: str, _session=Depends(require_auth)):
    """Retry sending a failed lead to Medidesk using saved data."""
    from app.lead_tracker import get_lead_event, log_lead_event, mark_retried

    event = get_lead_event(lead_id, status="failed")
    if not event:
        return JSONResponse(status_code=404, content={"error": "No failed event found for this lead"})

    mapped_values = event.get("mapped_values", {})
    medidesk_form_id = event.get("medidesk_form_id", "")

    if not mapped_values:
        return JSONResponse(status_code=400, content={
            "error": "No saved mapped values to retry",
            "fb_raw_data": event.get("fb_raw_data", {}),
        })

    if not medidesk_form_id:
        return JSONResponse(status_code=400, content={"error": "No Medidesk form ID saved"})

    # Retry the submission
    result = await submit_form_urlencoded(medidesk_form_id, mapped_values)

    if result.success:
        mark_retried(lead_id)
        log_lead_event(
            integration_id=event["integration_id"],
            lead_id=lead_id,
            status="sent",
            mapped_fields_count=len(mapped_values),
            fb_raw_data=event.get("fb_raw_data", {}),
            mapped_values=mapped_values,
            medidesk_form_id=medidesk_form_id,
        )
        return {"status": "sent", "lead_id": lead_id, "message": "Retry successful"}

    error_msg = f"Medidesk HTTP {result.status_code}"
    log_lead_event(
        integration_id=event["integration_id"],
        lead_id=lead_id,
        status="failed",
        mapped_fields_count=len(mapped_values),
        error=f"Retry failed: {error_msg}",
        fb_raw_data=event.get("fb_raw_data", {}),
        mapped_values=mapped_values,
        medidesk_form_id=medidesk_form_id,
    )
    return JSONResponse(status_code=502, content={
        "status": "failed",
        "lead_id": lead_id,
        "error": error_msg,
    })



# ─── Pages: Demo, Setup Wizard & Dashboard ────────────────────────


@app.get("/demo/contact", response_class=HTMLResponse, include_in_schema=False)
async def demo_contact_page():
    if not settings.demo_page_enabled:
        return HTMLResponse(
            "<p>Demo disabled. Set <code>MEDIDESK_DEMO_PAGE_ENABLED=true</code>.</p>",
            status_code=404,
        )
    path = Path(__file__).resolve().parent / "demo_contact.html"
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_wizard_page():
    """Serve the integration setup wizard."""
    path = Path(__file__).resolve().parent / "setup_wizard.html"
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page():
    """Serve the admin dashboard."""
    path = Path(__file__).resolve().parent / "dashboard.html"
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(content=html)

