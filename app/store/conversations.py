"""Conversation-level queries and customer matching updates."""

from app.store.database import get_db


async def get_all_conversations() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM conversations ORDER BY last_message_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_unmatched_conversations() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM conversations WHERE match_status = 'unmatched'"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_customer_match(
    phone: str, customer_id: str, customer_name: str, status: str = "matched"
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            UPDATE conversations
            SET customer_id = ?, customer_name = ?, match_status = ?
            WHERE phone = ?
            """,
            (customer_id, customer_name, status, phone),
        )
        await db.commit()


async def get_active_phones_today(since_ts: int) -> list[str]:
    """Return phone numbers that have unprocessed messages since a timestamp."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT phone FROM messages
            WHERE processed = 0 AND timestamp >= ?
            """,
            (since_ts,),
        )
        rows = await cursor.fetchall()
        return [row["phone"] for row in rows]
