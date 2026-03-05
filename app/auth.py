"""Shared admin auth dependency + login/logout routes."""

import hashlib
import hmac

from fastapi import APIRouter, Cookie, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings

router = APIRouter(tags=["auth"])

# ── Session cookie ────────────────────────────────────────────────────

_COOKIE_NAME = "crm_session"


def _session_signature() -> str:
    """Deterministic signature derived from the admin token (never stored raw)."""
    return hashlib.sha256(f"crm-session:{settings.admin_token}".encode()).hexdigest()


# ── Auth dependency ───────────────────────────────────────────────────

async def verify_admin(
    request: Request,
    authorization: str = Header(default=""),
    admin_token: str = Query(default="", alias="admin_token"),
) -> None:
    """Verify admin identity via cookie, header, or query param."""
    if not settings.admin_token:
        return  # no token configured = skip auth

    # 1. Cookie (login page sets this)
    cookie_val = request.cookies.get(_COOKIE_NAME, "")
    if cookie_val and hmac.compare_digest(cookie_val, _session_signature()):
        return

    # 2. Authorization header (API calls from JS)
    header_token = authorization.removeprefix("Bearer ").strip()
    if header_token and hmac.compare_digest(header_token, settings.admin_token):
        return

    # 3. Query param (legacy / convenience)
    if admin_token and hmac.compare_digest(admin_token, settings.admin_token):
        return

    # For HTML page requests, redirect to login instead of 401
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    raise HTTPException(status_code=401, detail="unauthorized")


# ── Login page ────────────────────────────────────────────────────────

_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — LOCACRYSTAL CRM</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:16px;padding:40px;width:360px;box-shadow:0 4px 24px rgba(0,0,0,.1);text-align:center}
h1{font-size:1.4rem;color:#0f172a;margin-bottom:8px}
p{font-size:.85rem;color:#64748b;margin-bottom:24px}
input{width:100%;padding:12px 16px;border:1px solid #d1d7db;border-radius:8px;font-size:15px;outline:none;margin-bottom:16px}
input:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.1)}
button{width:100%;padding:12px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;transition:background .15s}
button:hover{background:#1d4ed8}
.err{color:#dc2626;font-size:.85rem;margin-bottom:12px;display:none}
</style></head><body>
<div class="card">
<h1>LOCACRYSTAL CRM</h1>
<p>Enter admin password to continue</p>
<div class="err" id="err">Password incorrect</div>
<form method="POST" action="/login">
<input type="password" name="password" placeholder="Password" autofocus required>
<button type="submit">Login</button>
</form>
</div>
<script>
if(location.search.includes('error=1'))document.getElementById('err').style.display='block';
</script>
</body></html>
"""


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return _LOGIN_HTML


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = form.get("password", "")
    if not settings.admin_token or (
        password and hmac.compare_digest(str(password), settings.admin_token)
    ):
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie(
            _COOKIE_NAME,
            _session_signature(),
            httponly=True,
            samesite="lax",
            max_age=86400 * 7,  # 7 days
        )
        return response
    return RedirectResponse("/login?error=1", status_code=303)


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(_COOKIE_NAME)
    return response
