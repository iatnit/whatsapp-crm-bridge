"""Message CRUD operations."""

import logging
from datetime import datetime, timezone

from app.store.database import get_db

logger = logging.getLogger(__name__)


async def save_message(
    *,
    wa_message_id: str,
    phone: str,
    display_name: str = "",
    direction: str,
    msg_type: str = "text",
    content: str = "",
    media_path: str = "",
    timestamp: int,
) -> bool:
    """Insert a message. Returns True if inserted, False if duplicate."""
    async with get_db() as db:
        try:
            await db.execute(
                """
                INSERT INTO messages
                    (wa_message_id, phone, display_name, direction,
                     msg_type, content, media_path, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (wa_message_id, phone, display_name, direction,
                 msg_type, content, media_path, timestamp),
            )
            await db.commit()
            logger.info(
                "Saved %s message %s from %s", direction, wa_message_id, phone
            )
            return True
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                logger.debug("Duplicate message %s, skipped", wa_message_id)
                return False
            raise


async def update_conversation(phone: str, display_name: str = "") -> None:
    """Upsert the conversations table after saving a message."""
    async with get_db() as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO conversations (phone, display_name, first_message_at, last_message_at, total_messages)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(phone) DO UPDATE SET
                display_name = CASE
                    WHEN excluded.display_name != '' THEN excluded.display_name
                    ELSE conversations.display_name
                END,
                last_message_at = excluded.last_message_at,
                total_messages = conversations.total_messages + 1
            """,
            (phone, display_name, now, now),
        )
        await db.commit()


async def get_unprocessed_messages(since_ts: int | None = None) -> list[dict]:
    """Get all unprocessed messages, optionally since a timestamp."""
    async with get_db() as db:
        if since_ts:
            cursor = await db.execute(
                "SELECT * FROM messages WHERE processed = 0 AND timestamp >= ? ORDER BY timestamp",
                (since_ts,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM messages WHERE processed = 0 ORDER BY timestamp"
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def mark_processed(message_ids: list[int]) -> None:
    """Mark messages as processed after analysis."""
    if not message_ids:
        return
    async with get_db() as db:
        placeholders = ",".join("?" for _ in message_ids)
        await db.execute(
            f"UPDATE messages SET processed = 1 WHERE id IN ({placeholders})",
            message_ids,
        )
        await db.commit()
        logger.info("Marked %d messages as processed", len(message_ids))


async def get_messages_by_phone(phone: str, limit: int = 200) -> list[dict]:
    """Get recent messages for a specific phone number."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM messages WHERE phone = ? ORDER BY timestamp DESC LIMIT ?",
            (phone, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
