from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.cors import CORSMiddleware

from app.config import settings
from app.fb_auth import require_admin, require_auth, require_facility, require_write_role, router as fb_auth_router
from app.fb_client import subscribe_page_to_webhooks
from app.integrations_store import (
    Facility,
    FieldMapping,
    create_facility,
    create_integration,
    delete_facility,
    delete_integration,
    get_all_facilities,
    get_all_integrations,
    get_facility,
    get_integration,
    get_integrations_by_facility,
    update_facility,
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


def _check_integration_access(integration, session: dict) -> bool:
    """Verify the session user owns this integration (or is admin)."""
    if session.get("role") == "admin":
        return True
    facility_id = session.get("facility_id", "")
    return bool(facility_id and integration.facility_id == facility_id)


def _audit(request: Request, session: dict, action: str, *, integration_id: str = "",
           before: dict | None = None, after: dict | None = None) -> None:
    """Thin wrapper over users_store.log_integration_action with request context."""
    try:
        from app.users_store import log_integration_action
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
        log_integration_action(
            action=action,
            integration_id=integration_id,
            facility_id=session.get("facility_id", "") or "",
            fb_user_id=session.get("fb_user_id", "") or (session.get("user") or {}).get("id", ""),
            fb_user_name=(session.get("user") or {}).get("name", ""),
            before=before,
            after=after,
            ip=ip,
            user_agent=ua,
        )
    except Exception:
        logger.warning("audit helper failed action=%s", action, exc_info=True)


def _integration_snapshot(i) -> dict:
    """Compact snapshot of integration for audit (token stripped)."""
    return {
        "id": i.id,
        "active": bool(i.active),
        "fb_page_id": i.fb_page_id,
        "fb_page_name": i.fb_page_name,
        "fb_form_id": i.fb_form_id,
        "fb_form_name": i.fb_form_name,
        "medidesk_form_id": i.medidesk_form_id,
        "medidesk_form_name": i.medidesk_form_name,
        "mappings_count": len(i.field_mappings),
        "mappings": [
            {"fb": m.fb_field, "md": m.medidesk_field} for m in i.field_mappings
        ],
        "facility_id": i.facility_id,
    }

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


@app.on_event("startup")
async def startup_db():
    """Initialize SQLite, migrate JSON data, and start token check scheduler."""
    import asyncio
    from app.db import get_connection, migrate_from_json
    get_connection()  # ensure tables are created
    migrate_from_json()  # import existing JSON data (idempotent)

    # Load persisted admin password hash (if changed via dashboard)
    pw_path = Path(settings.data_dir) / ".admin_password"
    if pw_path.exists():
        try:
            saved_pw = pw_path.read_text(encoding="utf-8").strip()
            if saved_pw:
                # Auto-migrate: if stored as plaintext (no $2b$ prefix), hash it now
                if not saved_pw.startswith("$2b$"):
                    import bcrypt
                    hashed = bcrypt.hashpw(saved_pw.encode(), bcrypt.gensalt()).decode()
                    pw_path.write_text(hashed, encoding="utf-8")
                    settings.admin_password = hashed
                    logger.info("Migrated admin password from plaintext to bcrypt hash")
                else:
                    settings.admin_password = saved_pw
                    logger.info("Loaded admin password hash from persistent storage")
        except Exception:
            logger.warning("Failed to load admin password file", exc_info=True)

    # Start background token health check (every 24h)
    async def _token_check_loop():
        from app.alerting import check_and_alert
        await asyncio.sleep(60)  # wait 1 min after startup
        while True:
            try:
                await check_and_alert()
            except Exception:
                logger.error("Token check failed", exc_info=True)
            await asyncio.sleep(86400)  # 24 hours

    asyncio.create_task(_token_check_loop())


# ─── Existing endpoints ───────────────────────────────────────────────


@app.get("/")
async def root(request: Request):
    """Redirect to the facility login page."""
    qs = request.scope.get("query_string", b"").decode("utf-8")
    url = f"/login?{qs}" if qs else "/login"
    return RedirectResponse(url=url)


@app.get("/static/icon.jpg")
async def static_icon():
    """Serve the application icon image."""
    icon_path = Path(__file__).parent / "MD_Integrator_V1.jpg"
    return FileResponse(icon_path, media_type="image/jpeg")


@app.get("/login")
async def login_page():
    """Landing page — login via Facebook, then navigate to setup or dashboard."""
    html_path = Path(__file__).parent / "landing.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/info")
async def api_info():
    return {
        "service": "Medidesk Integrator",
        "version": "2.0.0",
        "docs": "/docs",
        "usage": "POST /api/submit/{medidesk_form_id} with JSON body of field values",
    }


@app.get("/debug/fb-config")
async def debug_fb_config(session: dict = Depends(require_admin)):
    """Admin-only diagnostic: verify all FB-related environment variables are set.

    Never returns secret values — only presence/length, so it is safe to read over HTTPS
    when troubleshooting why Facebook webhooks are rejected on production.
    """
    def _status(val: str) -> dict[str, Any]:
        return {"set": bool(val), "length": len(val) if val else 0}

    integrations = get_all_integrations()
    active = [i for i in integrations if i.active]

    return {
        "fb_app_id": _status(settings.fb_app_id),
        "fb_app_secret": _status(settings.fb_app_secret),
        "fb_webhook_verify_token": _status(settings.fb_webhook_verify_token),
        "fb_redirect_uri": settings.fb_redirect_uri,
        "fb_graph_version": settings.fb_graph_version,
        "encryption_key": _status(settings.encryption_key),
        "integrations_total": len(integrations),
        "integrations_active": len(active),
        "active_pages": sorted({i.fb_page_id for i in active if i.fb_page_id}),
        "blockers": [
            msg for msg in [
                "MEDIDESK_FB_APP_SECRET is NOT set — all webhooks return 403"
                if not settings.fb_app_secret else "",
                "MEDIDESK_FB_APP_ID is NOT set — OAuth/Graph calls will fail"
                if not settings.fb_app_id else "",
                "MEDIDESK_ENCRYPTION_KEY is NOT set — page tokens cannot be decrypted"
                if not settings.encryption_key else "",
                "No active integrations — webhook has no page to route leads to"
                if not active else "",
            ] if msg
        ],
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
async def create_new_integration(request: Request, _session=Depends(require_write_role)):
    """Create a new FB→Medidesk integration with field mappings."""
    try:
        body = await request.json()
    except Exception as e:
        logger.warning("POST /api/integrations: invalid JSON body: %s", e)
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    required = ["fb_page_id", "fb_page_name",
                 "fb_form_id", "fb_form_name",
                 "medidesk_form_id", "field_mappings"]

    for field in required:
        if field not in body:
            return JSONResponse(status_code=400, content={"error": f"Missing required field: {field}"})

    # Normalize + validate field_mappings (filter empty medidesk_field — wizard may push placeholders)
    raw_mappings = body.get("field_mappings") or []
    if not isinstance(raw_mappings, list):
        return JSONResponse(status_code=400, content={"error": "field_mappings must be a list"})

    valid_mappings = []
    for idx, m in enumerate(raw_mappings):
        if not isinstance(m, dict):
            continue
        fb_field = (m.get("fb_field") or "").strip()
        medidesk_field = (m.get("medidesk_field") or "").strip()
        if not fb_field or not medidesk_field:
            continue  # skip incomplete mapping rows
        try:
            confidence = float(m.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        valid_mappings.append(FieldMapping(
            fb_field=fb_field,
            medidesk_field=medidesk_field,
            confidence=confidence,
        ))

    if not valid_mappings:
        return JSONResponse(
            status_code=400,
            content={"error": "Brak kompletnych mapowań pól — zmapuj przynajmniej jedno pole FB na pole Medidesk."},
        )

    # Securely retrieve the page token from FB API using user's access token
    try:
        from app.fb_client import get_user_pages
        user_token = _session.get("access_token", "")
        if not user_token:
            return JSONResponse(status_code=401, content={"error": "Sesja wygasła lub nie zawiera tokenu FB — zaloguj się ponownie."})
        pages = await get_user_pages(user_token)
    except Exception as e:
        logger.exception("POST /api/integrations: failed to fetch FB pages")
        return JSONResponse(status_code=502, content={"error": f"Nie udało się pobrać Stron FB: {e}"})

    fb_page_token = None
    for p in pages:
        if p.page_id == body["fb_page_id"]:
            fb_page_token = p.access_token
            break

    if not fb_page_token:
        return JSONResponse(
            status_code=403,
            content={"error": "Brak dostępu do tej Strony FB (nie udało się pobrać tokenu). Sprawdź, czy Twoje konto FB ma rolę admina na tej Stronie."},
        )

    try:
        integration = create_integration(
            fb_page_id=body["fb_page_id"],
            fb_page_name=body["fb_page_name"],
            fb_page_token=fb_page_token,
            fb_form_id=body["fb_form_id"],
            fb_form_name=body["fb_form_name"],
            fb_form_questions=body.get("fb_form_questions") or [],
            medidesk_form_id=body["medidesk_form_id"],
            medidesk_form_name=body.get("medidesk_form_name", "") or "",
            medidesk_fields=body.get("medidesk_fields") or [],
            field_mappings=valid_mappings,
            facility_id=_session.get("facility_id", "") or "",
            name=(body.get("name") or "").strip(),
        )
    except Exception as e:
        logger.exception("POST /api/integrations: create_integration failed")
        return JSONResponse(status_code=500, content={"error": f"Nie udało się zapisać integracji: {e}"})

    _audit(request, _session, "integration.create", integration_id=integration.id,
           after=_integration_snapshot(integration))
    return {"status": "created", "integration": asdict(integration)}


@app.get("/api/integrations")
async def list_integrations(_session=Depends(require_auth)):
    """List all integrations (tokens hidden). Filtered by facility for non-admin."""
    role = _session.get("role", "user")
    facility_id = _session.get("facility_id", "")

    if role == "admin":
        integrations = get_all_integrations()
    elif facility_id:
        integrations = get_integrations_by_facility(facility_id)
    else:
        integrations = []  # unregistered user sees nothing

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
                "facility_id": i.facility_id,
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
    if not _check_integration_access(integration, _session):
        return JSONResponse(status_code=403, content={"error": "Brak dostępu do tej integracji"})
    data = asdict(integration)
    data.pop("fb_page_token", None)  # Never expose token via API
    return data


@app.put("/api/integrations/{integration_id}/mappings")
async def update_mappings(integration_id: str, request: Request, _session=Depends(require_write_role)):
    """Update field mappings for an existing integration."""
    body = await request.json()
    if "field_mappings" not in body:
        return JSONResponse(status_code=400, content={"error": "Missing field_mappings"})

    from app.integrations_store import FieldMapping, get_integration, update_integration

    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})
    if not _check_integration_access(integration, _session):
        return JSONResponse(status_code=403, content={"error": "Brak dostępu do tej integracji"})

    before = _integration_snapshot(integration)

    mappings = []
    for m in body["field_mappings"]:
        fb = (m.get("fb_field") or "").strip()
        md = (m.get("medidesk_field") or "").strip()
        if not fb or not md:
            continue
        mappings.append(FieldMapping(fb_field=fb, medidesk_field=md, confidence=float(m.get("confidence", 0.0) or 0.0)))

    updated = update_integration(integration_id, field_mappings=mappings)
    _audit(request, _session, "integration.update_mappings", integration_id=integration_id,
           before=before, after=_integration_snapshot(updated))

    data = asdict(updated)
    data.pop("fb_page_token", None)
    return {"status": "success", "integration": data}


@app.post("/api/integrations/{integration_id}/activate")
async def activate_integration(integration_id: str, request: Request, _session=Depends(require_write_role)):
    """Activate an integration and subscribe page to webhooks."""
    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})
    if not _check_integration_access(integration, _session):
        return JSONResponse(status_code=403, content={"error": "Brak dostępu do tej integracji"})

    # Subscribe page to webhooks
    success = await subscribe_page_to_webhooks(
        integration.fb_page_id, integration.fb_page_token
    )

    if not success:
        _audit(request, _session, "integration.activate_failed", integration_id=integration_id)
        return JSONResponse(
            status_code=502,
            content={"error": "Failed to subscribe page to webhooks. Check FB permissions."},
        )

    update_integration(integration_id, active=True)
    _audit(request, _session, "integration.activate", integration_id=integration_id,
           after={"active": True})
    return {"status": "activated", "integration_id": integration_id}


@app.post("/api/integrations/{integration_id}/deactivate")
async def deactivate_integration(integration_id: str, request: Request, _session=Depends(require_write_role)):
    """Deactivate an integration."""
    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})
    if not _check_integration_access(integration, _session):
        return JSONResponse(status_code=403, content={"error": "Brak dostępu do tej integracji"})
    update_integration(integration_id, active=False)
    _audit(request, _session, "integration.deactivate", integration_id=integration_id,
           after={"active": False})
    return {"status": "deactivated", "integration_id": integration_id}


@app.delete("/api/integrations/{integration_id}")
async def remove_integration(integration_id: str, request: Request, _session=Depends(require_admin)):
    """Delete an integration."""
    integration = get_integration(integration_id)
    before = _integration_snapshot(integration) if integration else None
    if delete_integration(integration_id):
        _audit(request, _session, "integration.delete", integration_id=integration_id, before=before)
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


# ─── Admin: users + audit timeline ────────────────────────────────


@app.get("/api/admin/users")
async def admin_list_users(_session=Depends(require_admin)):
    """List all users across facilities (admin only)."""
    from app.users_store import list_users
    users = list_users()
    # Enrich with facility name
    facilities = {f.id: f.name for f in get_all_facilities()}
    return {
        "users": [
            {
                "fb_user_id": u.fb_user_id,
                "fb_user_name": u.fb_user_name,
                "email": u.email,
                "facility_id": u.facility_id,
                "facility_name": facilities.get(u.facility_id, ""),
                "role": u.role,
                "label": u.label,
                "first_seen_at": u.first_seen_at,
                "last_seen_at": u.last_seen_at,
                "active": u.active,
            }
            for u in users
        ]
    }


@app.get("/api/admin/users/facility/{facility_id}")
async def admin_list_users_by_facility(facility_id: str, _session=Depends(require_facility)):
    """List users of a given facility. Non-admin can only query own facility."""
    from app.users_store import list_users
    if _session.get("role") != "admin" and _session.get("facility_id") != facility_id:
        return JSONResponse(status_code=403, content={"error": "Brak dostępu do tej placówki"})
    users = list_users(facility_id=facility_id)
    return {
        "users": [
            {
                "fb_user_id": u.fb_user_id,
                "fb_user_name": u.fb_user_name,
                "email": u.email,
                "role": u.role,
                "label": u.label,
                "last_seen_at": u.last_seen_at,
                "active": u.active,
            }
            for u in users
        ]
    }


@app.put("/api/admin/users/{fb_user_id}/role")
async def admin_set_user_role(fb_user_id: str, request: Request, _session=Depends(require_admin)):
    """Change role (owner/admin/viewer). Admin only."""
    from app.users_store import set_role
    body = await request.json()
    role = body.get("role", "")
    try:
        ok = set_role(fb_user_id, role)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not ok:
        return JSONResponse(status_code=404, content={"error": "User not found"})
    _audit(request, _session, "user.set_role", after={"fb_user_id": fb_user_id, "role": role})
    return {"status": "ok"}


@app.put("/api/admin/users/{fb_user_id}/facility")
async def admin_assign_user_facility(fb_user_id: str, request: Request, _session=Depends(require_admin)):
    """Assign user to a facility with a role (approves pending registration). Admin only."""
    from app.users_store import set_facility
    body = await request.json()
    facility_id = body.get("facility_id", "")
    role = body.get("role", "viewer")
    if not facility_id:
        return JSONResponse(status_code=400, content={"error": "facility_id required"})
    try:
        ok = set_facility(fb_user_id, facility_id, role)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not ok:
        return JSONResponse(status_code=404, content={"error": "User not found"})
    # Remove from pending_registrations
    try:
        from app.db import get_connection
        conn = get_connection()
        conn.execute("DELETE FROM pending_registrations WHERE fb_user_id = ?", (fb_user_id,))
        conn.commit()
    except Exception:
        logger.warning("Failed to clear pending_registrations", exc_info=True)
    _audit(request, _session, "user.assign_facility",
           after={"fb_user_id": fb_user_id, "facility_id": facility_id, "role": role})
    return {"status": "ok"}


@app.put("/api/admin/users/{fb_user_id}/label")
async def admin_set_user_label(fb_user_id: str, request: Request, _session=Depends(require_admin)):
    """Set admin-editable label (e.g. 'Aga — marketing'). Admin only."""
    from app.users_store import set_label
    body = await request.json()
    label = body.get("label", "")
    ok = set_label(fb_user_id, label)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "User not found"})
    return {"status": "ok"}


@app.post("/api/admin/users/{fb_user_id}/deactivate")
async def admin_deactivate_user(fb_user_id: str, request: Request, _session=Depends(require_admin)):
    """Soft-disable user (blocks future logins; keeps audit trail)."""
    from app.users_store import deactivate
    ok = deactivate(fb_user_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "User not found"})
    _audit(request, _session, "user.deactivate", after={"fb_user_id": fb_user_id})
    return {"status": "ok"}


@app.get("/api/admin/audit")
async def admin_audit_timeline(
    facility_id: str = "",
    fb_user_id: str = "",
    limit: int = 100,
    offset: int = 0,
    _session=Depends(require_facility),
):
    """Audit timeline — who did what when. Non-admin sees only own facility."""
    from app.users_store import list_audit
    # Scope non-admin to their own facility
    if _session.get("role") != "admin":
        facility_id = _session.get("facility_id", "")
        if not facility_id:
            return {"events": []}
    limit = max(1, min(500, int(limit or 100)))
    events = list_audit(
        facility_id=facility_id or None,
        fb_user_id=fb_user_id or None,
        limit=limit,
        offset=max(0, int(offset or 0)),
    )
    return {"events": events, "limit": limit, "offset": offset}


@app.get("/api/admin/pending")
async def admin_list_pending(_session=Depends(require_admin)):
    """List FB users who logged in but have no facility assigned (awaiting approval)."""
    from app.db import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT fb_user_id, fb_user_name, attempted_at FROM pending_registrations ORDER BY attempted_at DESC"
    ).fetchall()
    return {
        "pending": [
            {
                "fb_user_id": r["fb_user_id"],
                "fb_user_name": r["fb_user_name"] or "",
                "attempted_at": r["attempted_at"],
            }
            for r in rows
        ]
    }


@app.get("/api/stats/{integration_id}")
async def integration_stats(integration_id: str, _session=Depends(require_auth)):
    """Stats for a specific integration."""
    from app.lead_tracker import get_stats, get_recent_leads

    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})
    if not _check_integration_access(integration, _session):
        return JSONResponse(status_code=403, content={"error": "Brak dostępu do tej integracji"})

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


# ─── Token health check ─────────────────────────────────────────


@app.get("/api/integrations/{integration_id}/token-status")
async def get_token_status(integration_id: str, _session=Depends(require_auth)):
    """Check FB token validity and expiration for an integration."""
    from app.alerting import debug_fb_token
    integration = get_integration(integration_id)
    if not integration:
        return JSONResponse(status_code=404, content={"error": "Integration not found"})
    status = await debug_fb_token(integration.fb_page_token)
    status["integration_id"] = integration_id
    status["fb_page_name"] = integration.fb_page_name
    return status


@app.get("/api/token-health")
async def get_all_token_health(_session=Depends(require_admin)):
    """Check all integration tokens (admin only)."""
    from app.alerting import check_all_tokens
    results = await check_all_tokens()
    return {"tokens": results, "warn_days": settings.token_expiry_warn_days}


# ─── Admin password management ───────────────────────────────────


@app.delete("/api/admin/reset-db")
async def reset_database(_session=Depends(require_admin)):
    """DANGER: Wipes all integrations, facilities, pending registrations, and sessions. Used for testing."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM integrations")
        conn.execute("DELETE FROM facilities")
        conn.execute("DELETE FROM pending_registrations")
        conn.execute("DELETE FROM sessions")
        conn.commit()
        return {"status": "ok", "message": "Database wiped successfully."}
    except Exception as e:
        logger.error("Error wiping DB: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.put("/api/admin/password")
async def change_admin_password(request: Request, _session=Depends(require_admin)):
    """Change the admin password (persisted as bcrypt hash)."""
    import bcrypt
    body = await request.json()
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")

    if not current or not new_pw:
        return JSONResponse(status_code=400, content={"error": "Podaj obecne i nowe hasło"})

    # Verify current password (supports both bcrypt hash and legacy plaintext)
    stored = settings.admin_password
    if stored.startswith("$2b$"):
        if not bcrypt.checkpw(current.encode(), stored.encode()):
            return JSONResponse(status_code=401, content={"error": "Obecne hasło jest nieprawidłowe"})
    else:
        import hmac
        if not hmac.compare_digest(current.encode(), stored.encode()):
            return JSONResponse(status_code=401, content={"error": "Obecne hasło jest nieprawidłowe"})

    if len(new_pw) < 6:
        return JSONResponse(status_code=400, content={"error": "Nowe hasło musi mieć co najmniej 6 znaków"})

    # Hash and persist
    hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    pw_path = Path(settings.data_dir) / ".admin_password"
    pw_path.write_text(hashed, encoding="utf-8")

    # Update in-memory setting
    settings.admin_password = hashed

    logger.info("Admin password changed successfully (bcrypt)")
    return {"status": "ok", "message": "Hasło zostało zmienione"}


# ─── Facility management (admin only) ────────────────────────────


@app.get("/api/facilities")
async def list_facilities(_session=Depends(require_admin)):
    """List all registered facilities (admin only)."""
    facilities = get_all_facilities()
    # Count integrations per facility
    all_integrations = get_all_integrations()
    facility_counts = {}
    for i in all_integrations:
        fid = i.facility_id or "__unassigned__"
        facility_counts[fid] = facility_counts.get(fid, 0) + 1

    return {
        "facilities": [
            {
                "id": f.id,
                "name": f.name,
                "fb_user_id": f.fb_user_id,
                "fb_user_name": f.fb_user_name,
                "created_at": f.created_at,
                "integrations_count": facility_counts.get(f.id, 0),
            }
            for f in facilities
        ],
        "unassigned_count": facility_counts.get("__unassigned__", 0),
    }


@app.post("/api/facilities")
async def create_new_facility(request: Request, _session=Depends(require_admin)):
    """Register a new facility (admin only)."""
    body = await request.json()
    name = body.get("name", "").strip()
    fb_user_id = body.get("fb_user_id", "").strip()

    if not name or not fb_user_id:
        return JSONResponse(status_code=400, content={"error": "Wymagane: name i fb_user_id"})

    from app.integrations_store import get_facility_by_fb_user
    existing = get_facility_by_fb_user(fb_user_id)
    if existing:
        return JSONResponse(status_code=409, content={"error": f"Placówka z tym FB user ID już istnieje: {existing.name}"})

    facility = create_facility(
        name=name,
        fb_user_id=fb_user_id,
        fb_user_name=body.get("fb_user_name", ""),
    )
    return {"status": "created", "facility": asdict(facility)}


@app.put("/api/facilities/{facility_id}")
async def update_existing_facility(facility_id: str, request: Request, _session=Depends(require_admin)):
    """Update facility name (admin only)."""
    body = await request.json()
    updated = update_facility(facility_id, name=body.get("name"))
    if not updated:
        return JSONResponse(status_code=404, content={"error": "Facility not found"})
    return {"status": "updated", "facility": asdict(updated)}


@app.delete("/api/facilities/{facility_id}")
async def delete_existing_facility(facility_id: str, _session=Depends(require_admin)):
    """Delete a facility (admin only)."""
    if delete_facility(facility_id):
        return {"status": "deleted"}
    return JSONResponse(status_code=404, content={"error": "Facility not found"})


@app.get("/api/facilities/pending")
async def list_pending_registrations(_session=Depends(require_admin)):
    """List FB users who tried to log in but are not registered (admin only)."""
    from app.db import get_connection
    conn = get_connection()
    rows = conn.execute("SELECT * FROM pending_registrations ORDER BY attempted_at DESC").fetchall()
    return {
        "pending": [
            {"fb_user_id": r["fb_user_id"], "fb_user_name": r["fb_user_name"], "attempted_at": r["attempted_at"]}
            for r in rows
        ]
    }


@app.post("/api/facilities/pending/{fb_user_id}/approve")
async def approve_pending(fb_user_id: str, request: Request, _session=Depends(require_admin)):
    """Approve a pending registration — creates a facility and removes from pending."""
    from app.db import get_connection
    body = await request.json()
    name = body.get("name", "").strip()

    if not name:
        return JSONResponse(status_code=400, content={"error": "Podaj nazwę placówki"})

    # Check not already registered
    from app.integrations_store import get_facility_by_fb_user
    existing = get_facility_by_fb_user(fb_user_id)
    if existing:
        return JSONResponse(status_code=409, content={"error": f"Już zarejestrowana: {existing.name}"})

    # Get pending info
    conn = get_connection()
    row = conn.execute("SELECT * FROM pending_registrations WHERE fb_user_id = ?", (fb_user_id,)).fetchone()
    fb_user_name = row["fb_user_name"] if row else ""

    # Create facility
    facility = create_facility(name=name, fb_user_id=fb_user_id, fb_user_name=fb_user_name)

    # Remove from pending
    conn.execute("DELETE FROM pending_registrations WHERE fb_user_id = ?", (fb_user_id,))
    conn.commit()

    return {"status": "approved", "facility": asdict(facility)}


@app.delete("/api/facilities/pending/{fb_user_id}")
async def dismiss_pending(fb_user_id: str, _session=Depends(require_admin)):
    """Dismiss a pending registration without approving."""
    from app.db import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM pending_registrations WHERE fb_user_id = ?", (fb_user_id,))
    conn.commit()
    return {"status": "dismissed"}


# ─── Pages: Demo, Setup Wizard, Dashboard & FB Compliance ────────


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    """Privacy policy required for Facebook App Review."""
    path = Path(__file__).parent / "privacy.html"
    return path.read_text(encoding="utf-8")


@app.get("/tos", response_class=HTMLResponse)
async def terms_of_service():
    """Terms of Service required for Facebook App Review."""
    path = Path(__file__).parent / "tos.html"
    return path.read_text(encoding="utf-8")


@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion():
    """Data deletion instructions required for Facebook App Review."""
    path = Path(__file__).parent / "data_deletion.html"
    return path.read_text(encoding="utf-8")


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
    """Serve the admin/facility dashboard."""
    path = Path(__file__).resolve().parent / "dashboard.html"
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page():
    """Serve the admin login page."""
    path = Path(__file__).resolve().parent / "admin_login.html"
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(content=html)
