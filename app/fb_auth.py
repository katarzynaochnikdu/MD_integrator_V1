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
COOKIE_MAX_AGE = 3 * 3600  # 3h — matches server-side SESSION_TTL (security policy)
SESSION_TTL = 3 * 3600  # 3 hours — sliding (measured from last_activity_at)
ACTIVITY_WRITE_THROTTLE = 60  # write last_activity_at to DB at most every 60s


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


def set_session_cookie(response: Response, session_id: str) -> None:
    """Store session ID in a signed cookie. All data stays server-side."""
    signed = _sign(session_id)
    response.set_cookie(
        COOKIE_NAME,
        signed,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,  # HTTPS only — required for production
    )


def get_session_from_cookie(request: Request) -> dict[str, Any] | None:
    """Extract session ID from cookie and look up session server-side."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    session_id = _verify(cookie)
    if not session_id:
        return None
    return _get_valid_session(session_id)


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
    """Save session to SQLite. Access token is encrypted at rest."""
    import json
    import time
    from app.db import get_connection
    from app.integrations_store import _get_fernet

    # Encrypt access_token at rest (if encryption key configured)
    raw_token = session_data.get("access_token", "")
    fernet = _get_fernet()
    encrypted_token = fernet.encrypt(raw_token.encode()).decode() if (raw_token and fernet) else raw_token

    # Strip page access_tokens from pages data (sensitive, not needed in session)
    pages_safe = [
        {"page_id": p.get("page_id", ""), "name": p.get("name", "")}
        for p in session_data.get("pages", [])
    ]

    now = time.time()
    fb_user_id = (session_data.get("user") or {}).get("id", "")
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO sessions
           (session_id, access_token, user_data, pages_data, role, facility_id, facility_name, created_at, last_activity_at, fb_user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            encrypted_token,
            json.dumps(session_data.get("user", {}), ensure_ascii=False),
            json.dumps(pages_safe, ensure_ascii=False),
            session_data.get("role", "user"),
            session_data.get("facility_id", ""),
            session_data.get("facility_name", ""),
            now,
            now,
            fb_user_id,
        ),
    )
    conn.commit()


def _touch_session_activity(session_id: str, last_seen: float | None) -> None:
    """Update last_activity_at (throttled — at most once per ACTIVITY_WRITE_THROTTLE seconds)."""
    import time
    now = time.time()
    if last_seen is not None and now - last_seen < ACTIVITY_WRITE_THROTTLE:
        return
    from app.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE sessions SET last_activity_at = ? WHERE session_id = ?", (now, session_id))
    conn.commit()


def audit_log(event: str, *, session_id: str = "", fb_user_id: str = "", request: Request | None = None) -> None:
    """Write an entry to session_audit (RODO-friendly login/logout trail)."""
    import time
    from app.db import get_connection
    ip = ""
    ua = ""
    if request is not None:
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")[:300]
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO session_audit (event, session_id, fb_user_id, ip, user_agent, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (event, session_id, fb_user_id, ip, ua, time.time()),
        )
        conn.commit()
    except Exception:
        logger.warning("audit_log failed for event=%s", event, exc_info=True)


def _cleanup_expired_sessions():
    """Remove sessions idle longer than SESSION_TTL (sliding expiry)."""
    import time
    from app.db import get_connection
    conn = get_connection()
    cutoff = time.time() - SESSION_TTL
    # Idle cutoff based on last_activity_at; fallback to created_at for legacy rows
    result = conn.execute(
        "DELETE FROM sessions WHERE COALESCE(last_activity_at, created_at) < ?",
        (cutoff,),
    )
    conn.commit()
    if result.rowcount > 0:
        logger.info("Cleaned up %d expired sessions", result.rowcount)


def _get_valid_session(session_id: str) -> dict | None:
    """Get session from SQLite if it exists and hasn't been idle longer than SESSION_TTL.

    Implements sliding expiry: TTL is measured from last_activity_at (falling back to
    created_at for legacy rows). On successful access, last_activity_at is touched —
    throttled so we don't hammer the DB on every request.
    """
    import json
    import time
    from app.db import get_connection
    from app.integrations_store import _get_fernet

    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row:
        return None

    cols = row.keys()
    last_activity = row["last_activity_at"] if "last_activity_at" in cols and row["last_activity_at"] is not None else row["created_at"]

    if time.time() - last_activity > SESSION_TTL:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        return None

    # Sliding refresh (throttled write)
    _touch_session_activity(session_id, last_activity)

    # Decrypt access_token
    encrypted_token = row["access_token"]
    fernet = _get_fernet()
    try:
        if fernet and encrypted_token:
            access_token = fernet.decrypt(encrypted_token.encode()).decode()
        else:
            access_token = encrypted_token or ""
    except Exception:
        access_token = ""  # corrupted token, session still valid for UI

    return {
        "access_token": access_token,
        "user": json.loads(row["user_data"]),
        "pages": json.loads(row["pages_data"]),
        "role": row["role"],
        "facility_id": row["facility_id"] if "facility_id" in cols else "",
        "facility_name": row["facility_name"] if "facility_name" in cols else "",
        "fb_user_id": row["fb_user_id"] if "fb_user_id" in cols else (json.loads(row["user_data"]).get("id", "") if row["user_data"] else ""),
    }


# ─── Endpoints ─────────────────────────────────────────────────────


@router.get("")
async def facebook_login(request: Request):
    """Redirect the user to Facebook OAuth login dialog.

    Smart-skip: if the user has a valid server-side session within the 3h sliding
    TTL, we do NOT round-trip through Facebook (which would show the "Reconnect"
    consent screen). Instead we go straight to the requested destination. This
    preserves the 3h security policy while removing friction inside that window.

    Pass ?force=1 to explicitly force re-auth with Facebook (e.g. to switch accounts).
    """
    redirect_to = request.query_params.get("redirect", "/dashboard")
    force = request.query_params.get("force", "") in ("1", "true", "yes")

    if not force:
        session = get_session_from_cookie(request)
        if session and session.get("role") in ("user", "admin"):
            audit_log("login_skip_oauth", session_id="", fb_user_id=session.get("fb_user_id", ""), request=request)
            return RedirectResponse(redirect_to)

    url = get_login_url(state=redirect_to)
    return RedirectResponse(url)


@router.get("/callback")
async def facebook_callback(request: Request):
    """Handle Facebook OAuth callback — exchange code for token."""
    try:
        return await _do_facebook_callback(request)
    except Exception as e:
        logger.exception("Facebook callback error")
        return JSONResponse(status_code=500, content={"error": str(e)})


async def _do_facebook_callback(request: Request):
    """Inner callback logic."""
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

    # Look up facility by FB user ID
    from app.integrations_store import get_facility_by_fb_user
    fb_user_id = user.get("id", "")
    facility = get_facility_by_fb_user(fb_user_id)
    facility_id = facility.id if facility else ""
    facility_name = facility.name if facility else ""
    role = "user" if facility else "unregistered"

    # Save unregistered attempt for admin to approve
    if role == "unregistered":
        try:
            from app.db import get_connection
            from datetime import datetime, timezone
            conn = get_connection()
            conn.execute(
                "INSERT OR REPLACE INTO pending_registrations (fb_user_id, fb_user_name, attempted_at) VALUES (?, ?, ?)",
                (fb_user_id, user.get("name", ""), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            logger.info("Saved pending registration for FB user %s (%s)", fb_user_id, user.get("name", ""))
        except Exception:
            logger.warning("Failed to save pending registration", exc_info=True)

    # Save session to SQLite (survives restarts)
    import uuid
    session_id = uuid.uuid4().hex  # cryptographically random, not guessable
    session_data = {
        "access_token": access_token,
        "user": user,
        "pages": pages_data,
        "role": role,
        "facility_id": facility_id,
        "facility_name": facility_name,
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
        redirect_url = f"/dashboard?fb_session={session_id}"

    # Audit — successful FB login
    audit_log("login_fb", session_id=session_id, fb_user_id=fb_user_id, request=request)

    # Set cookie (stores only session_id, not sensitive data) and redirect
    response = RedirectResponse(redirect_url)
    set_session_cookie(response, session_id)
    return response


@router.get("/session/{session_id}")
async def get_session(session_id: str, request: Request):
    """Get stored session data (user info, pages) — for setup wizard. Requires matching cookie."""
    # Verify that the requester owns this session (cookie must match)
    cookie_session = get_session_from_cookie(request)
    cookie_raw = request.cookies.get(COOKIE_NAME)
    cookie_sid = _verify(cookie_raw) if cookie_raw else None
    if cookie_sid != session_id and (not cookie_session or cookie_session.get("role") != "admin"):
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    
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
        "facility_id": session.get("facility_id", ""),
        "facility_name": session.get("facility_name", ""),
    }


@router.post("/logout")
async def logout(request: Request):
    """Clear session cookie and remove the current server-side session."""
    from app.db import get_connection
    cookie_raw = request.cookies.get(COOKIE_NAME)
    sid = _verify(cookie_raw) if cookie_raw else None
    fb_user_id = ""
    if sid:
        sess = _get_valid_session(sid)
        if sess:
            fb_user_id = sess.get("fb_user_id", "") or (sess.get("user") or {}).get("id", "")
        try:
            conn = get_connection()
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
            conn.commit()
        except Exception:
            logger.warning("Failed to delete session on logout", exc_info=True)
    audit_log("logout", session_id=sid or "", fb_user_id=fb_user_id, request=request)

    response = JSONResponse(content={"status": "logged_out"})
    response.delete_cookie(COOKIE_NAME)
    return response


@router.post("/logout-all")
async def logout_all(request: Request):
    """Revoke ALL sessions for the current user across devices.

    Use when a device is lost/stolen. Requires a valid session to identify the user.
    """
    from app.db import get_connection
    session = get_session_from_cookie(request)
    if not session:
        return JSONResponse(status_code=401, content={"error": "Nie zalogowany"})
    fb_user_id = session.get("fb_user_id", "") or (session.get("user") or {}).get("id", "")
    if not fb_user_id:
        return JSONResponse(status_code=400, content={"error": "Brak identyfikatora użytkownika"})

    try:
        conn = get_connection()
        cur = conn.execute("DELETE FROM sessions WHERE fb_user_id = ?", (fb_user_id,))
        conn.commit()
        revoked = cur.rowcount
    except Exception:
        logger.exception("logout-all failed")
        return JSONResponse(status_code=500, content={"error": "Logout-all failed"})

    audit_log("logout_all", fb_user_id=fb_user_id, request=request)

    response = JSONResponse(content={"status": "all_revoked", "revoked": revoked})
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
    stored = settings.admin_password
    # Support both bcrypt hash ($2b$...) and legacy plaintext passwords
    if stored.startswith("$2b$"):
        import bcrypt
        if not bcrypt.checkpw(password.encode(), stored.encode()):
            return JSONResponse(status_code=401, content={"error": "Nieprawidłowe hasło"})
    else:
        if not hmac.compare_digest(password.encode(), stored.encode()):
            return JSONResponse(status_code=401, content={"error": "Nieprawidłowe hasło"})

    session_data = {
        "user": {"name": "Administrator", "id": "admin"},
        "pages": [],
        "role": "admin",
    }
    # Save admin session to SQLite
    import uuid
    admin_session_id = uuid.uuid4().hex
    _save_session(admin_session_id, session_data)

    audit_log("login_admin", session_id=admin_session_id, fb_user_id="admin", request=request)
    response = JSONResponse(content={"status": "ok", "role": "admin"})
    set_session_cookie(response, admin_session_id)
    return response


@router.get("/pages/{session_id}/{page_id}/forms")
async def get_page_forms(session_id: str, page_id: str):
    """Get Lead Ad forms for a specific page from a session."""
    from app.fb_client import get_page_lead_forms, get_user_pages

    session = _get_valid_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": "Session not found or expired"})

    # Verify user has access to this page, then get fresh page token from FB API
    access_token = session.get("access_token", "")
    if not access_token:
        return JSONResponse(status_code=401, content={"error": "No valid access token"})

    pages = await get_user_pages(access_token)
    page_token = None
    for p in pages:
        if p.page_id == page_id:
            page_token = p.access_token
            break

    if not page_token:
        return JSONResponse(status_code=404, content={"error": f"Page {page_id} not found or no access"})

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
