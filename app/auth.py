"""Shared admin auth dependency for FastAPI endpoints."""

import hmac

from fastapi import Header, HTTPException

from app.config import settings


async def verify_admin(
    authorization: str = Header(default=""),
) -> None:
    """Verify admin token for management endpoints.

    Accepts: Authorization: Bearer <token> header only.
    Skipped if ADMIN_TOKEN is not configured.
    """
    if not settings.admin_token:
        return  # no token configured = skip auth
    header_token = authorization.removeprefix("Bearer ").strip()
    if header_token and hmac.compare_digest(header_token, settings.admin_token):
        return
    raise HTTPException(status_code=401, detail="unauthorized")
