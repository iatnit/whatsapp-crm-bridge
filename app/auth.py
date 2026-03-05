"""Shared admin auth dependency for FastAPI endpoints."""

import hmac

from fastapi import Header, HTTPException, Query

from app.config import settings


async def verify_admin(
    authorization: str = Header(default=""),
    admin_token: str = Query(default="", alias="admin_token"),
) -> None:
    """Verify admin token for management endpoints.

    Accepts: Authorization: Bearer <token> header, or ?admin_token=<token> query param.
    Skipped if ADMIN_TOKEN is not configured.
    """
    if not settings.admin_token:
        return  # no token configured = skip auth
    header_token = authorization.removeprefix("Bearer ").strip()
    if header_token and hmac.compare_digest(header_token, settings.admin_token):
        return
    if admin_token and hmac.compare_digest(admin_token, settings.admin_token):
        return
    raise HTTPException(status_code=401, detail="unauthorized")
