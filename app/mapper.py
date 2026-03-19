from __future__ import annotations

from typing import Any

from app.config import settings
from app.schemas import ContactRequest


def build_medidesk_payload(
    req: ContactRequest,
    attachment_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Map ContactRequest to the Medidesk form submission payload."""

    fields: dict[str, str] = {
        "Imię-i-Nazwisko": req.full_name,
        "Telefon": req.phone,
        "E-mail": req.email or "",
        "W-czym-możemy-pomóc-": req.topic.value,
        "Dodatkowa-informacja": req.message,
        "Wyrażam-zgodę-na-kontakt-zwrotny-telefonicznie-lub-mailowo-": (
            "true" if req.consent else "false"
        ),
    }

    payload: dict[str, Any] = {
        "siteDomain": req.site_domain or settings.default_site_domain,
        "siteUrl": req.site_url or settings.default_site_url,
        "fieldsValues": fields,
    }

    if attachment_ids:
        payload["attachments"] = {"Dodaj-zdjęcie": attachment_ids}

    return payload
