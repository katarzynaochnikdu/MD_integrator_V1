"""Facebook OAuth authentication endpoints with cookie-based sessions."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import base64
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.fb_client import (
    exchange_code_for_token,
    get_login_url,
    get_long_lived_token,
    get_user_info,
    get_user_pages,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/facebook", tags=["Facebook Auth"])

COOKIE_NAME = "fb_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days


# ─── Signed cookie helpers ─────────────────────────────────────────


def _sign(payload: str) -> str:
    """Create HMAC signature for cookie data."""
    sig = hmac.new(
        settings.fb_session_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return f"{payload}.{sig}"


def _verify(signed: str) -> str | None:
    """Verify and extract cookie data, return None if invalid."""
    if "." not in signed:
        return None
    payload, sig = signed.rsplit(".", 1)
    expected = hmac.new(
        settings.fb_session_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected):
        return None
    return payload


def set_session_cookie(response: Response, data: dict[str, Any]) -> None:
    """Encode session data into a signed cookie."""
    raw = base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()
    signed = _sign(raw)
    response.set_cookie(
        COOKIE_NAME,
        signed,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # set True in production with HTTPS
    )


def get_session_from_cookie(request: Request) -> dict[str, Any] | None:
    """Extract and verify session data from cookie."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    payload = _verify(cookie)
    if not payload:
        return None
    try:
        return json.loads(base64.b64decode(payload))
    except Exception:
        return None


def get_current_user(request: Request) -> dict[str, Any] | None:
    """Get current user from session cookie. Returns None if not logged in."""
    session = get_session_from_cookie(request)
    if not session:
        return None
    return session.get("user")


def get_session_role(request: Request) -> str | None:
    """Get current session role ('admin' or 'user'). None if not logged in."""
    session = get_session_from_cookie(request)
    if not session:
        return None
    return session.get("role")


# ─── Auth dependencies for FastAPI ────────────────────────────────


async def require_auth(request: Request) -> dict[str, Any]:
    """FastAPI dependency: require a valid session (cookie or in-memory session ID)."""
    # 1. Try signed cookie first
    session = get_session_from_cookie(request)
    if session and "user" in session:
        return session

    # 2. Try in-memory session via header or query param
    session_id = (
        request.headers.get("X-Session-Id")
        or request.query_params.get("fb_session")
    )
    if session_id:
        mem_session = _get_valid_session(session_id)
        if mem_session and "user" in mem_session:
            return mem_session

    raise HTTPException(status_code=401, detail="Nie zalogowany")


async def require_admin(request: Request) -> dict[str, Any]:
    """FastAPI dependency: require admin role."""
    session = await require_auth(request)
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Wymagane uprawnienia administratora")
    return session


# ─── Rate limiting (in-memory) ────────────────────────────────────

_login_attempts: dict[str, list[float]] = {}  # ip -> [timestamp, ...]
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 5  # max attempts per window


def _check_rate_limit(ip: str) -> bool:
    """Returns True if request is allowed, False if rate-limited."""
    import time
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Clean old attempts
    attempts = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    _login_attempts[ip] = attempts
    if len(attempts) >= RATE_LIMIT_MAX:
        return False
    attempts.append(now)
    return True


# Session storage — SQLite-backed (survives restarts)
SESSION_TTL = 3 * 3600  # 3 hours in seconds


def _save_session(session_id: str, session_data: dict) -> None:
    """Save session to SQLite."""
    import json
    import time
    from app.db import get_connection
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO sessions
           (session_id, access_token, user_data, pages_data, role, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            session_data.get("access_token", ""),
            json.dumps(session_data.get("user", {}), ensure_ascii=False),
            json.dumps(session_data.get("pages", []), ensure_ascii=False),
            session_data.get("role", "user"),
            time.time(),
        ),
    )
    conn.commit()


def _cleanup_expired_sessions():
    """Remove sessions older than SESSION_TTL."""
    import time
    from app.db import get_connection
    conn = get_connection()
    cutoff = time.time() - SESSION_TTL
    result = conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
    conn.commit()
    if result.rowcount > 0:
        logger.info("Cleaned up %d expired sessions", result.rowcount)


def _get_valid_session(session_id: str) -> dict | None:
    """Get session from SQLite if it exists and hasn't expired."""
    import json
    import time
    from app.db import get_connection
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row:
        return None
    if time.time() - row["created_at"] > SESSION_TTL:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        return None
    return {
        "access_token": row["access_token"],
        "user": json.loads(row["user_data"]),
        "pages": json.loads(row["pages_data"]),
        "role": row["role"],
    }


# ─── Endpoints ─────────────────────────────────────────────────────


@router.get("")
async def facebook_login(request: Request):
    """Redirect the user to Facebook OAuth login dialog."""
    # Capture the intended redirect destination
    redirect_to = request.query_params.get("redirect", "/dashboard")
    url = get_login_url(state=redirect_to)
    return RedirectResponse(url)


@router.get("/callback")
async def facebook_callback(request: Request):
    """Handle Facebook OAuth callback — exchange code for token."""
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    fb_state = request.query_params.get("state", "/dashboard")

    if error:
        logger.warning("FB OAuth error: %s — %s", error, request.query_params.get("error_description", ""))
        return JSONResponse(
            status_code=400,
            content={"error": error, "description": request.query_params.get("error_description", "")},
        )

    if not code:
        return JSONResponse(status_code=400, content={"error": "No authorization code received"})

    # Exchange code for short-lived token
    token_data = await exchange_code_for_token(code)
    if "error" in token_data:
        return JSONResponse(status_code=400, content={"error": "Token exchange failed", "details": token_data["error"]})

    short_token = token_data.get("access_token", "")

    # Exchange for long-lived token
    ll_data = await get_long_lived_token(short_token)
    access_token = ll_data.get("access_token", short_token)

    # Get user info
    user = await get_user_info(access_token)

    # Get user pages
    pages = await get_user_pages(access_token)

    pages_data = [{"page_id": p.page_id, "name": p.name, "access_token": p.access_token} for p in pages]

    # Save session to SQLite (survives restarts)
    session_id = user.get("id", "unknown")
    session_data = {
        "access_token": access_token,
        "user": user,
        "pages": pages_data,
        "role": "user",
    }
    _save_session(session_id, session_data)

    # Cleanup old sessions opportunistically
    _cleanup_expired_sessions()

    # Determine redirect target
    if fb_state.startswith("/setup"):
        redirect_url = f"/setup?fb_session={session_id}"
    elif fb_state == "/" or fb_state == "":
        redirect_url = f"/?fb_session={session_id}"
    else:
        redirect_url = "/dashboard"

    # Set cookie and redirect
    response = RedirectResponse(redirect_url)
    set_session_cookie(response, session_data)
    return response


@router.get("/session/{session_id}")
async def get_session(session_id: str):
    """Get stored session data (user info, pages) — for setup wizard."""
    session = _get_valid_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": "Session not found or expired"})
    return {
        "user": session["user"],
        "pages": session["pages"],
    }


@router.get("/me")
async def get_current_user_endpoint(request: Request):
    """Get current logged-in user from cookie session."""
    session = get_session_from_cookie(request)
    if not session:
        return JSONResponse(status_code=401, content={"error": "Not logged in"})
    return {
        "user": session["user"],
        "pages": session.get("pages", []),
        "role": session.get("role", "user"),
    }


@router.post("/logout")
async def logout(request: Request):
    """Clear session cookie."""
    response = JSONResponse(content={"status": "logged_out"})
    response.delete_cookie(COOKIE_NAME)
    return response


# ─── Admin auth ────────────────────────────────────────────────────


@router.post("/admin/login")
async def admin_login(request: Request):
    """Authenticate admin with password (rate-limited)."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": "Zbyt wiele prób logowania. Spróbuj za minutę."},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    password = body.get("password", "")
    if password != settings.admin_password:
        return JSONResponse(status_code=401, content={"error": "Nieprawidłowe hasło"})

    session_data = {
        "user": {"name": "Administrator", "id": "admin"},
        "pages": [],
        "role": "admin",
    }
    response = JSONResponse(content={"status": "ok", "role": "admin"})
    set_session_cookie(response, session_data)
    return response


@router.get("/pages/{session_id}/{page_id}/forms")
async def get_page_forms(session_id: str, page_id: str):
    """Get Lead Ad forms for a specific page from a session."""
    from app.fb_client import get_page_lead_forms

    session = _sessions.get(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": "Session not found"})

    # Find the page token
    page_token = None
    for p in session["pages"]:
        if p["page_id"] == page_id:
            page_token = p["access_token"]
            break

    if not page_token:
        return JSONResponse(status_code=404, content={"error": f"Page {page_id} not found in session"})

    forms = await get_page_lead_forms(page_id, page_token)
    return {
        "page_id": page_id,
        "forms": [
            {
                "form_id": f.form_id,
                "name": f.name,
                "status": f.status,
                "leads_count": f.leads_count,
                "questions": f.questions,
                "created_time": f.created_time,
            }
            for f in forms
        ],
    }
