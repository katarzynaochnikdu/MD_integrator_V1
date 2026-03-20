from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class MedideskResult:
    success: bool
    status_code: int
    body: dict[str, Any] | None = None
    raw_text: str | None = None


@dataclass
class FormField:
    field_id: str
    field_type: str
    required: bool
    name: str
    options: list[str] | None = None


async def fetch_form_fields(form_id: str) -> list[FormField]:
    """GET /api/forms/{form_id} — pobiera aktualną definicję pól z Medidesk."""
    url = f"{settings.medidesk_api_base}/{form_id}"
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url)

    if resp.status_code != 200:
        logger.warning("Medidesk GET form %s status=%s", form_id, resp.status_code)
        return []

    data = resp.json()
    return [
        FormField(
            field_id=f["fieldId"],
            field_type=f["type"],
            required=f.get("required", False),
            name=f.get("name", f["fieldId"]),
            options=f.get("options"),
        )
        for f in data.get("fields", [])
    ]


def build_urlencoded_body(
    fields_values: dict[str, str],
    site_domain: str | None = None,
    site_url: str | None = None,
) -> str:
    """Buduje body w formacie fieldsValues[fieldId]=value (urlencoded, ASCII-safe)."""
    parts: list[str] = [
        f"siteDomain={quote(site_domain or settings.default_site_domain, safe='')}",
        f"siteUrl={quote(site_url or settings.default_site_url, safe='')}",
    ]
    for key, value in fields_values.items():
        parts.append(f"fieldsValues[{key}]={quote(str(value), safe='')}")
    return "&".join(parts)


async def submit_form_urlencoded(
    form_id: str,
    fields_values: dict[str, str],
    site_domain: str | None = None,
    site_url: str | None = None,
) -> MedideskResult:
    """POST urlencoded do Medidesk — bez captchy."""
    url = f"{settings.medidesk_api_base}/{form_id}"
    body = build_urlencoded_body(fields_values, site_domain, site_url)

    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        try:
            resp = await client.post(
                url,
                content=body.encode("ascii"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException:
            logger.warning("Medidesk request timed out")
            return MedideskResult(success=False, status_code=504)
        except httpx.HTTPError as exc:
            logger.error("Medidesk HTTP error: %s", exc)
            return MedideskResult(success=False, status_code=502)

    response_body = None
    raw_text = (resp.text or "")[:8000] if resp.text else None
    try:
        response_body = resp.json()
    except Exception:
        pass

    if resp.status_code != 200:
        logger.info(
            "Medidesk POST form=%s status=%s body=%s",
            form_id,
            resp.status_code,
            (resp.text or "")[:1200],
        )

    return MedideskResult(
        success=resp.status_code == 200,
        status_code=resp.status_code,
        body=response_body,
        raw_text=raw_text,
    )
