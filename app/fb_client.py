"""Facebook Graph API client for Lead Ads integration."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"


@dataclass
class FBPage:
    page_id: str
    name: str
    access_token: str


@dataclass
class FBLeadForm:
    form_id: str
    name: str
    status: str
    leads_count: int
    questions: list[dict[str, Any]]
    created_time: str = ""


@dataclass
class FBLead:
    lead_id: str
    created_time: str
    field_data: dict[str, str]
    ad_id: str | None = None
    ad_name: str | None = None
    adset_name: str | None = None
    campaign_name: str | None = None
    platform: str | None = None
    is_organic: bool | None = None


def _graph_url(path: str) -> str:
    return f"{GRAPH_BASE}/{settings.fb_graph_version}/{path}"


def _extract_consent_questions(form: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull consent checkboxes out of FB Lead Form `legal_content` + `gdpr_consent`.

    FB stores compliance/consent checkboxes separately from regular `questions`,
    which means our setup wizard never showed them — but they DO arrive in the
    lead's `field_data` payload, so without surfacing them the user has no way
    to map them onto Medidesk fields. This helper synthesizes question entries
    so the wizard can display + map them like any other field.

    Each returned entry carries:
      - `key`           — matches the field_data name FB sends with the lead
      - `label`         — checkbox text shown to the lead
      - `type`          — "CHECKBOX" so type-compat treats it like a bool
      - `is_consent`    — UI flag: render with 🔒 icon, suggest `true` constant
      - `is_required`   — if FB says the checkbox MUST be checked
      - `consent_body`  — full disclaimer text the lead actually saw (may be
                          long — UI exposes it via tooltip + copy-to-clipboard
                          so the user can paste it into a Medidesk note field
                          for compliance records).
    """
    out: list[dict[str, Any]] = []

    # Path 1: legal_content.custom_disclaimer.checkboxes — most common shape.
    legal = (form.get("legal_content") or {})
    disclaimer = legal.get("custom_disclaimer") or form.get("custom_disclaimer") or {}
    body = (disclaimer.get("body") or "").strip()
    title = (disclaimer.get("title") or "").strip()
    full_text = "\n".join(p for p in (title, body) if p)
    for cb in (disclaimer.get("checkboxes") or []):
        key = cb.get("key") or cb.get("name") or ""
        if not key:
            continue
        out.append({
            "key": key,
            "label": cb.get("label") or cb.get("text") or key,
            "type": "CHECKBOX",
            "is_consent": True,
            "is_required": bool(cb.get("is_required") or cb.get("required")),
            "consent_body": full_text,
        })

    # Path 2: gdpr_consent.custom_consent[] — newer EU-region forms put GDPR
    # checkboxes here, each with its own body. Same fields, different source.
    gdpr = form.get("gdpr_consent") or {}
    for c in (gdpr.get("custom_consent") or []):
        key = c.get("key") or c.get("name") or ""
        if not key:
            continue
        out.append({
            "key": key,
            "label": c.get("label") or c.get("text") or key,
            "type": "CHECKBOX",
            "is_consent": True,
            "is_required": bool(c.get("is_required") or c.get("required")),
            "consent_body": (c.get("body") or c.get("description") or "").strip(),
        })

    return out


def get_login_url(state: str = "") -> str:
    """Generate Facebook OAuth login URL with required permissions."""
    params = {
        "client_id": settings.fb_app_id,
        "redirect_uri": settings.fb_redirect_uri,
        "scope": ",".join([
            "pages_show_list",
            "pages_read_engagement",
            "pages_manage_ads",
            "pages_manage_metadata",
            "leads_retrieval",
            "business_management",
        ]),
        "response_type": "code",
        "state": state,
    }
    return f"https://www.facebook.com/{settings.fb_graph_version}/dialog/oauth?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict[str, Any]:
    """Exchange OAuth authorization code for an access token."""
    url = _graph_url("oauth/access_token")
    params = {
        "client_id": settings.fb_app_id,
        "client_secret": settings.fb_app_secret,
        "redirect_uri": settings.fb_redirect_uri,
        "code": code,
    }
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url, params=params)

    if resp.status_code != 200:
        logger.error("FB token exchange failed: %s %s", resp.status_code, resp.text[:500])
        return {"error": resp.text}

    return resp.json()


async def get_long_lived_token(short_token: str) -> dict[str, Any]:
    """Exchange a short-lived token for a long-lived one (60 days)."""
    url = _graph_url("oauth/access_token")
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": settings.fb_app_id,
        "client_secret": settings.fb_app_secret,
        "fb_exchange_token": short_token,
    }
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url, params=params)

    if resp.status_code != 200:
        logger.error("FB long-lived token exchange failed: %s", resp.text[:500])
        return {"error": resp.text}

    return resp.json()


async def get_user_info(access_token: str) -> dict[str, Any]:
    """Get basic info about the logged-in Facebook user."""
    url = _graph_url("me")
    params = {"fields": "id,name,email", "access_token": access_token}
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url, params=params)
    if resp.status_code != 200:
        return {"error": resp.text}
    return resp.json()


async def get_user_pages(access_token: str) -> list[FBPage]:
    """Get Facebook Pages the user manages (direct + via Business Manager)."""
    seen_ids: set[str] = set()
    pages: list[FBPage] = []

    # 1. Direct pages (me/accounts) — pages where user is admin directly
    url = _graph_url("me/accounts")
    params = {"fields": "id,name,access_token", "access_token": access_token}
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url, params=params)

    if resp.status_code == 200:
        data = resp.json()
        for p in data.get("data", []):
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                pages.append(FBPage(
                    page_id=p["id"],
                    name=p["name"],
                    access_token=p["access_token"],
                ))
    else:
        logger.error("FB get pages (me/accounts) failed: %s", resp.text[:500])

    # 2. Business Manager pages (owned + client)
    businesses = await get_user_businesses(access_token)
    for biz in businesses:
        biz_pages = await get_business_pages(biz["id"], access_token)
        for p in biz_pages:
            if p.page_id not in seen_ids:
                seen_ids.add(p.page_id)
                pages.append(p)

    logger.info("Total pages found: %d (direct + business)", len(pages))
    return pages


async def get_user_businesses(access_token: str) -> list[dict[str, Any]]:
    """Get businesses the user has access to via Business Manager."""
    url = _graph_url("me/businesses")
    params = {"fields": "id,name", "access_token": access_token}
    businesses: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url, params=params)

    if resp.status_code != 200:
        logger.warning("FB get businesses failed (status=%s): %s", resp.status_code, resp.text[:300])
        return businesses

    data = resp.json()
    businesses = data.get("data", [])
    logger.info("Found %d businesses for user", len(businesses))
    return businesses


async def get_business_pages(business_id: str, access_token: str) -> list[FBPage]:
    """Get all pages (owned + client) for a Business Manager account."""
    pages: list[FBPage] = []
    endpoints = [
        f"{business_id}/owned_pages",
        f"{business_id}/client_pages",
    ]
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        for endpoint in endpoints:
            url = _graph_url(endpoint)
            params = {"fields": "id,name,access_token", "access_token": access_token}
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                for p in data.get("data", []):
                    token = p.get("access_token", "")
                    if token:
                        pages.append(FBPage(
                            page_id=p["id"],
                            name=p.get("name", f"Page {p['id']}"),
                            access_token=token,
                        ))
                    else:
                        logger.warning("Page %s from %s has no access_token — skipping", p["id"], endpoint)
            else:
                logger.warning("FB %s failed (status=%s): %s", endpoint, resp.status_code, resp.text[:300])
    return pages


async def get_page_lead_forms(page_id: str, page_token: str) -> list[FBLeadForm]:
    """Get Lead Ad forms for a Facebook Page, sorted newest-first.

    Pulls `legal_content`, `custom_disclaimer` and `gdpr_consent` alongside
    the regular `questions` so the wizard can surface consent checkboxes the
    user otherwise never sees during config (Make.com pulls these by default
    too — that's why consents appeared there but not here).
    """
    url = _graph_url(f"{page_id}/leadgen_forms")
    params = {
        "fields": (
            "id,name,status,leads_count,questions,created_time,"
            "legal_content,custom_disclaimer,gdpr_consent,privacy_policy"
        ),
        "access_token": page_token,
    }
    forms: list[FBLeadForm] = []
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url, params=params)

    if resp.status_code != 200:
        logger.error("FB get lead forms failed: %s", resp.text[:500])
        return forms

    data = resp.json()
    for f in data.get("data", []):
        raw_status = f.get("status", "")
        # Merge real questions with synthesized consent-checkbox entries so the
        # wizard sees one unified list. Consent items carry `is_consent: true`
        # which the UI uses to render the 🔒 icon + body tooltip.
        questions = list(f.get("questions") or [])
        consent_questions = _extract_consent_questions(f)
        questions.extend(consent_questions)

        logger.info(
            "Form id=%s name=%s status=%s leads=%s created=%s questions=%d consents=%d",
            f["id"], f.get("name", ""), raw_status,
            f.get("leads_count", 0), f.get("created_time", ""),
            len(f.get("questions") or []), len(consent_questions),
        )
        forms.append(FBLeadForm(
            form_id=f["id"],
            name=f.get("name", ""),
            status=raw_status or "UNKNOWN",
            leads_count=f.get("leads_count", 0),
            questions=questions,
            created_time=f.get("created_time", ""),
        ))

    # Sort newest first
    forms.sort(key=lambda f: f.created_time, reverse=True)
    return forms


async def get_lead_data(lead_id: str, access_token: str) -> FBLead | None:
    """Fetch a specific lead's data from Facebook."""
    url = _graph_url(lead_id)
    params = {
        "fields": "id,created_time,field_data,ad_id,ad_name,adset_name,campaign_name,platform,is_organic",
        "access_token": access_token,
    }
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.get(url, params=params)

    if resp.status_code != 200:
        logger.error("FB get lead failed: %s", resp.text[:500])
        return None

    data = resp.json()
    field_data = {}
    for fd in data.get("field_data", []):
        field_data[fd["name"]] = fd["values"][0] if fd.get("values") else ""

    return FBLead(
        lead_id=data["id"],
        created_time=data.get("created_time", ""),
        field_data=field_data,
        ad_id=data.get("ad_id"),
        ad_name=data.get("ad_name"),
        adset_name=data.get("adset_name"),
        campaign_name=data.get("campaign_name"),
        platform=data.get("platform"),
        is_organic=data.get("is_organic"),
    )


async def subscribe_page_to_webhooks(page_id: str, page_token: str) -> bool:
    """Subscribe a Page to leadgen webhooks (install the app on the page)."""
    url = _graph_url(f"{page_id}/subscribed_apps")
    params = {
        "subscribed_fields": "leadgen",
        "access_token": page_token,
    }
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        resp = await client.post(url, params=params)

    if resp.status_code != 200:
        logger.error("FB webhook subscribe failed: %s", resp.text[:500])
        return False

    result = resp.json()
    return result.get("success", False)
