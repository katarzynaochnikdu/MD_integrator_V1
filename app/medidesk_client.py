from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_ATTACHMENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text",
    "text/plain",
}

MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB


@dataclass
class MedideskResult:
    success: bool
    status_code: int
    body: dict[str, Any] | None = None


async def submit_form(
    payload: dict[str, Any],
    captcha_token: str,
) -> MedideskResult:
    """POST the mapped payload to the Medidesk forms endpoint."""

    headers = {
        "Content-Type": "application/json",
        "captcha-response": captcha_token,
    }

    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        try:
            resp = await client.post(
                settings.medidesk_form_url,
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException:
            logger.warning("Medidesk request timed out")
            return MedideskResult(success=False, status_code=504)
        except httpx.HTTPError as exc:
            logger.error("Medidesk HTTP error: %s", exc)
            return MedideskResult(success=False, status_code=502)

    body = None
    try:
        body = resp.json()
    except Exception:
        pass

    return MedideskResult(
        success=resp.status_code == 200,
        status_code=resp.status_code,
        body=body,
    )


async def upload_attachment(file_bytes: bytes, filename: str) -> str | None:
    """Upload a single file to the Medidesk attachments endpoint.

    Returns the attachment UUID on success, or None on failure.
    """

    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        try:
            resp = await client.post(
                settings.medidesk_attachments_url,
                files={"file": (filename, file_bytes)},
            )
        except httpx.HTTPError as exc:
            logger.error("Attachment upload failed: %s", exc)
            return None

    if resp.status_code != 200:
        logger.warning(
            "Attachment upload returned %d: %s", resp.status_code, resp.text
        )
        return None

    data = resp.json()
    return data.get("id")
