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


@dataclass
class FormDefinition:
    name: str
    fields: list[FormField]


async def fetch_form_definition(form_id: str) -> FormDefinition | None:
    """GET /api/forms/{form_id} — pobiera nazwę i pola formularza z Medidesk."""
    url = f"{settings.medidesk_api_base}/{form_id}"
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url)

    if resp.status_code != 200:
        logger.warning("Medidesk GET form %s status=%s", form_id, resp.status_code)
        return None

    data = resp.json()
    fields = [
        FormField(
            field_id=f["fieldId"],
            field_type=f["type"],
            required=f.get("required", False),
            name=f.get("name", f["fieldId"]),
            options=f.get("options"),
        )
        for f in data.get("fields", [])
    ]
    return FormDefinition(name=data.get("name", ""), fields=fields)


def build_urlencoded_body(
    fields_values: dict[str, str],
    site_domain: str | None = None,
    site_url: str | None = None,
) -> str:
    """Buduje body w formacie fieldsValues[fieldId]=value (urlencoded, ASCII-safe).

    Both keys and values are percent-encoded (UTF-8) so fieldIds with Polish
    diacritics like "Imię-i-nazwisko" don't leak raw `ę` into the request body.
    The `[` / `]` around fieldsValues stay literal — they're part of PHP-style
    array-param syntax and every standard parser (including Medidesk) decodes
    the key name back from its percent-encoded form.
    """
    parts: list[str] = [
        f"siteDomain={quote(site_domain or settings.default_site_domain, safe='')}",
        f"siteUrl={quote(site_url or settings.default_site_url, safe='')}",
    ]
    for key, value in fields_values.items():
        parts.append(
            f"fieldsValues[{quote(str(key), safe='')}]={quote(str(value), safe='')}"
        )
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
                # Body is guaranteed pure ASCII after build_urlencoded_body percent-encodes
                # every key + value, but we still encode as UTF-8 as defense-in-depth —
                # if a future caller passes raw bytes by accident, UTF-8 won't blow up
                # on non-ASCII characters the way .encode("ascii") did (see Polish 'ę').
                content=body.encode("utf-8"),
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
        # Log BOTH Medidesk's response AND the body we sent — without the
        # request body it's impossible to tell why their API rejected the lead
        # (value too long? wrong format? missing required?). Body is already
        # URL-encoded so values are non-readable secrets-style.
        logger.warning(
            "Medidesk POST form=%s status=%s response=%s sent_body=%s",
            form_id,
            resp.status_code,
            (resp.text or "")[:1200],
            body[:2000],
        )

    return MedideskResult(
        success=resp.status_code == 200,
        status_code=resp.status_code,
        body=response_body,
        raw_text=raw_text,
    )
