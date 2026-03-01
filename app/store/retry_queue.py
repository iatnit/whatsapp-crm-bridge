"""Persistent retry queue for failed CRM writes.

Stores failed Feishu/HubSpot write operations in SQLite so they can be
retried on the next pipeline run instead of being silently dropped.
"""

import json
import logging
from datetime import datetime, timezone

from app.store.database import get_db

logger = logging.getLogger(__name__)

# Max retry attempts before giving up
MAX_RETRIES = 3


async def init_retry_table() -> None:
    """Create the retry queue table if it doesn't exist."""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS retry_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                target      TEXT NOT NULL,       -- 'feishu' or 'hubspot'
                operation   TEXT NOT NULL,       -- 'ensure_customer', 'ensure_followup', 'ensure_contact', 'ensure_note'
                args_json   TEXT NOT NULL,       -- JSON-serialized function arguments
                error_msg   TEXT DEFAULT '',
                retries     INTEGER DEFAULT 0,
                created_at  DATETIME NOT NULL,
                last_retry  DATETIME,
                status      TEXT DEFAULT 'pending'  -- pending / success / failed
            )
        """)
        await db.commit()


async def enqueue(
    target: str,
    operation: str,
    args: dict,
    error_msg: str = "",
) -> int | None:
    """Add a failed write to the retry queue.

    Args:
        target: 'feishu' or 'hubspot'
        operation: Function name like 'ensure_customer'
        args: Dict of function arguments (must be JSON-serializable)
        error_msg: The error message from the failed write

    Returns the queue item ID or None.
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO retry_queue (target, operation, args_json, error_msg, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (target, operation, json.dumps(args, ensure_ascii=False),
             error_msg, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        item_id = cursor.lastrowid
        logger.info("Enqueued retry: %s.%s (id=%d)", target, operation, item_id)
        return item_id


async def get_pending() -> list[dict]:
    """Get all pending retry items (under MAX_RETRIES)."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT * FROM retry_queue
            WHERE status = 'pending' AND retries < ?
            ORDER BY created_at
            """,
            (MAX_RETRIES,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def mark_success(item_id: int) -> None:
    """Mark a retry item as successfully completed."""
    async with get_db() as db:
        await db.execute(
            "UPDATE retry_queue SET status = 'success', last_retry = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), item_id),
        )
        await db.commit()


async def mark_retried(item_id: int, error_msg: str = "") -> None:
    """Increment retry count and update error message."""
    async with get_db() as db:
        await db.execute(
            """
            UPDATE retry_queue
            SET retries = retries + 1, error_msg = ?, last_retry = ?,
                status = CASE WHEN retries + 1 >= ? THEN 'failed' ELSE 'pending' END
            WHERE id = ?
            """,
            (error_msg, datetime.now(timezone.utc).isoformat(), MAX_RETRIES, item_id),
        )
        await db.commit()


async def cleanup_old(days: int = 7) -> int:
    """Delete completed/failed items older than N days. Returns count deleted."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            DELETE FROM retry_queue
            WHERE status IN ('success', 'failed')
              AND created_at < datetime('now', ?)
            """,
            (f"-{days} days",),
        )
        await db.commit()
        return cursor.rowcount or 0
