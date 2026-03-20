from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.config import settings
from app.medidesk_client import (
    MedideskResult,
    fetch_form_fields,
    submit_form_urlencoded,
)

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
    fields = await fetch_form_fields(form_id)
    if not fields:
        return JSONResponse(
            status_code=404,
            content={"error": f"Form {form_id} not found or has no fields"},
        )
    return {
        "form_id": form_id,
        "fields": [
            {
                "fieldId": f.field_id,
                "type": f.field_type,
                "required": f.required,
                "name": f.name,
                "options": f.options,
            }
            for f in fields
        ],
    }


@app.post("/api/submit/{form_id}")
async def submit_to_medidesk(form_id: str, request: Request):
    """Generyczny endpoint: przyjmuje JSON z polami, wysyła urlencoded do Medidesk.

    Body JSON: klucze = fieldId z Medidesk, wartości = stringi.
    Opcjonalnie: siteDomain, siteUrl (nadpisują domyślne).
    """
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
