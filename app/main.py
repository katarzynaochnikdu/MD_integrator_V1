from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.config import settings
from app.mapper import build_medidesk_payload
from app.medidesk_client import (
    MedideskResult,
    submit_form,
    upload_attachment,
    get_placeholder_attachment_id,
    MAX_ATTACHMENT_SIZE,
    ALLOWED_ATTACHMENT_TYPES,
)
from app.schemas import (
    ContactRequest,
    SuccessResponse,
    ValidationErrorResponse,
    CaptchaErrorResponse,
    FieldError,
    UpstreamErrorResponse,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Medidesk Integrator", version="1.0.0")


@app.get("/")
async def root():
    """Render i przeglądarki często odpytują GET / – bez tej trasy byłby 404."""
    return {
        "service": "Medidesk Integrator",
        "docs": "/docs",
        "demo_contact": "/demo/contact",
        "api_contact": "/api/medidesk/contact",
    }


def _upstream_error_response(result: MedideskResult) -> JSONResponse:
    """502 z opcjonalnymi szczegółami, gdy MEDIDESK_DEBUG_UPSTREAM=true."""
    kw: dict = {
        "message": f"Medidesk returned HTTP {result.status_code}",
    }
    if settings.debug_upstream:
        kw["upstream_status"] = result.status_code
        if result.body is not None:
            kw["upstream_body"] = result.body
        elif result.raw_text:
            kw["upstream_preview"] = result.raw_text[:2000]
    body = UpstreamErrorResponse(**kw).model_dump(by_alias=True, exclude_none=True)
    status = result.status_code if result.status_code in (502, 504) else 502
    return JSONResponse(status_code=status, content=body)

if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/demo/contact", response_class=HTMLResponse, include_in_schema=False)
async def demo_contact_page():
    """Strona testowa: reCAPTCHA + POST na /api/medidesk/contact bez ręcznego kopiowania tokenu.

    Włącz na Renderze: MEDIDESK_DEMO_PAGE_ENABLED=true
    """
    if not settings.demo_page_enabled:
        return HTMLResponse(
            "<p>Strona demo wyłączona. Ustaw <code>MEDIDESK_DEMO_PAGE_ENABLED=true</code>.</p>",
            status_code=404,
        )
    path = Path(__file__).resolve().parent / "demo_contact.html"
    html = path.read_text(encoding="utf-8")
    html = html.replace("__RECAPTCHA_SITE_KEY__", settings.recaptcha_site_key)
    return HTMLResponse(content=html)


@app.post(
    "/api/medidesk/contact",
    response_model=SuccessResponse,
    responses={
        400: {"model": ValidationErrorResponse},
        401: {"model": CaptchaErrorResponse},
        502: {"model": UpstreamErrorResponse},
        504: {"model": UpstreamErrorResponse},
    },
)
async def submit_contact(req: ContactRequest):
    """Accept contact data and forward it to the Medidesk forms API."""

    attachment_ids: list[str] | None = None
    if settings.auto_placeholder_photo:
        pid = await get_placeholder_attachment_id(req.captcha_token)
        if pid:
            attachment_ids = [pid]
        else:
            logger.warning(
                "Upload placeholder PNG failed; wysyłam bez załącznika (możliwy błąd 500 po stronie Medidesk)"
            )

    payload = build_medidesk_payload(req, attachment_ids=attachment_ids)
    result = await submit_form(payload, req.captcha_token)

    if result.success:
        return SuccessResponse()

    if result.status_code == 401:
        return JSONResponse(
            status_code=401,
            content=CaptchaErrorResponse().model_dump(),
        )

    if result.status_code == 400 and result.body:
        resp = ValidationErrorResponse(
            global_errors=[
                FieldError(**e) for e in result.body.get("globalErrors", [])
            ],
            field_errors={
                field: [FieldError(**e) for e in errors]
                for field, errors in result.body.get("fieldErrors", {}).items()
            },
        )
        return JSONResponse(status_code=400, content=resp.model_dump(by_alias=True))

    return _upstream_error_response(result)


@app.post(
    "/api/medidesk/contact-with-attachment",
    response_model=SuccessResponse,
    responses={
        400: {"model": ValidationErrorResponse},
        401: {"model": CaptchaErrorResponse},
        502: {"model": UpstreamErrorResponse},
    },
)
async def submit_contact_with_attachment(
    data: str = Form(..., description="JSON string matching ContactRequest schema"),
    file: UploadFile = File(...),
):
    """Accept contact data + file attachment and forward to Medidesk.

    The contact data is sent as a JSON string in the `data` form field
    because multipart/form-data cannot carry nested JSON natively.
    """

    import json
    from pydantic import ValidationError

    try:
        req = ContactRequest.model_validate_json(data)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content=exc.errors())

    if file.content_type and file.content_type not in ALLOWED_ATTACHMENT_TYPES:
        return JSONResponse(
            status_code=400,
            content=ValidationErrorResponse(
                global_errors=[FieldError(code="invalid_file_type")],
            ).model_dump(by_alias=True),
        )

    file_bytes = await file.read()
    if len(file_bytes) > MAX_ATTACHMENT_SIZE:
        return JSONResponse(
            status_code=400,
            content=ValidationErrorResponse(
                global_errors=[FieldError(code="file_too_large")],
            ).model_dump(by_alias=True),
        )

    attachment_id = await upload_attachment(
        file_bytes, file.filename or "attachment", captcha_token=req.captcha_token
    )
    if not attachment_id:
        attachment_id = await upload_attachment(
            file_bytes, file.filename or "attachment", captcha_token=None
        )
    if not attachment_id:
        return JSONResponse(
            status_code=502,
            content=UpstreamErrorResponse(
                message="Failed to upload attachment to Medidesk"
            ).model_dump(by_alias=True, exclude_none=True),
        )

    payload = build_medidesk_payload(req, attachment_ids=[attachment_id])
    result = await submit_form(payload, req.captcha_token)

    if result.success:
        return SuccessResponse()

    if result.status_code == 401:
        return JSONResponse(
            status_code=401,
            content=CaptchaErrorResponse().model_dump(),
        )

    if result.status_code == 400 and result.body:
        resp = ValidationErrorResponse(
            global_errors=[
                FieldError(**e) for e in result.body.get("globalErrors", [])
            ],
            field_errors={
                field: [FieldError(**e) for e in errors]
                for field, errors in result.body.get("fieldErrors", {}).items()
            },
        )
        return JSONResponse(status_code=400, content=resp.model_dump(by_alias=True))

    return _upstream_error_response(result)
