"""Operation audit log for admin actions."""

import logging

from app.store.database import get_db

logger = logging.getLogger(__name__)


async def log_action(action: str, target: str = "", details: str = "") -> None:
    """Record an admin action in the audit log."""
    try:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO audit_log (action, target, details) VALUES (?, ?, ?)",
                (action, target, details),
            )
            await db.commit()
    except Exception:
        logger.warning("Failed to write audit log: %s %s", action, target)


async def get_recent_logs(limit: int = 200) -> list[dict]:
    """Return recent audit log entries."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, action, target, details, created_at "
            "FROM audit_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]
