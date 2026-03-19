from __future__ import annotations

import logging

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse

from app.mapper import build_medidesk_payload
from app.medidesk_client import (
    submit_form,
    upload_attachment,
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

    payload = build_medidesk_payload(req)
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

    return JSONResponse(
        status_code=result.status_code if result.status_code in (502, 504) else 502,
        content=UpstreamErrorResponse(
            message=f"Medidesk returned HTTP {result.status_code}"
        ).model_dump(),
    )


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

    attachment_id = await upload_attachment(file_bytes, file.filename or "attachment")
    if not attachment_id:
        return JSONResponse(
            status_code=502,
            content=UpstreamErrorResponse(
                message="Failed to upload attachment to Medidesk"
            ).model_dump(),
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

    return JSONResponse(
        status_code=502,
        content=UpstreamErrorResponse(
            message=f"Medidesk returned HTTP {result.status_code}"
        ).model_dump(),
    )
