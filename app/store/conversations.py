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


async def update_hubspot_id(phone: str, hubspot_contact_id: str) -> None:
    """Store HubSpot contact ID in the conversations table."""
    async with get_db() as db:
        await db.execute(
            "UPDATE conversations SET hubspot_contact_id = ? WHERE phone = ?",
            (hubspot_contact_id, phone),
        )
        await db.commit()


async def get_sync_status() -> dict:
    """Get cross-system CRM sync status for all conversations.

    Returns counts and a list of conversations with missing CRM links.
    """
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM conversations")
        total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM conversations "
            "WHERE customer_id != '' AND customer_id IS NOT NULL"
        )
        feishu_linked = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM conversations "
            "WHERE hubspot_contact_id != '' AND hubspot_contact_id IS NOT NULL"
        )
        hubspot_linked = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM conversations "
            "WHERE (customer_id != '' AND customer_id IS NOT NULL) "
            "  AND (hubspot_contact_id != '' AND hubspot_contact_id IS NOT NULL)"
        )
        both_linked = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT phone, display_name, customer_name, customer_id, "
            "       hubspot_contact_id, match_status "
            "FROM conversations "
            "WHERE (customer_id = '' OR customer_id IS NULL) "
            "   OR (hubspot_contact_id = '' OR hubspot_contact_id IS NULL) "
            "ORDER BY last_message_at DESC LIMIT 50"
        )
        gaps = [dict(row) for row in await cursor.fetchall()]

    return {
        "total_conversations": total,
        "feishu_linked": feishu_linked,
        "hubspot_linked": hubspot_linked,
        "both_linked": both_linked,
        "feishu_only": feishu_linked - both_linked,
        "hubspot_only": hubspot_linked - both_linked,
        "neither": total - feishu_linked - hubspot_linked + both_linked,
        "gaps": gaps,
    }


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
