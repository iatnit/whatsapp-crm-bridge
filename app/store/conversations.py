"""Conversation-level queries and customer matching updates."""

import time

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


async def is_ai_disabled(phone: str) -> bool:
    """Check if AI auto-reply is disabled for this phone."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT ai_disabled FROM conversations WHERE phone = ?",
            (phone,),
        )
        row = await cursor.fetchone()
        return bool(row and row["ai_disabled"])


async def set_ai_disabled(phone: str, disabled: bool) -> bool:
    """Enable or disable AI auto-reply for a phone. Returns True if row existed."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE conversations SET ai_disabled = ? WHERE phone = ?",
            (1 if disabled else 0, phone),
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_customer_size(phone: str, size: str) -> bool:
    """Set customer size (big/medium/small or empty). Returns True if row existed."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE conversations SET customer_size = ? WHERE phone = ?",
            (size, phone),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_ai_disabled_list() -> list[dict]:
    """Return all customers with AI auto-reply disabled."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT phone, display_name, customer_name "
            "FROM conversations WHERE ai_disabled = 1 "
            "ORDER BY last_message_at DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_customer_context(phone: str) -> dict:
    """Build customer context from local SQLite data (zero API calls).

    Returns a dict with:
      is_known, customer_name, relationship_stage, total_messages, first_seen_days
    """
    async with get_db() as db:
        # Conversation-level data
        cursor = await db.execute(
            "SELECT customer_name, match_status, total_messages, first_message_at "
            "FROM conversations WHERE phone = ?",
            (phone,),
        )
        conv = await cursor.fetchone()

        if not conv:
            return {
                "is_known": False,
                "customer_name": "",
                "relationship_stage": "new",
                "total_messages": 0,
                "first_seen_days": 0,
            }

        total = conv["total_messages"] or 0
        customer_name = conv["customer_name"] or ""
        is_known = conv["match_status"] == "matched" and bool(customer_name)

        # Calculate days since first contact
        first_at = conv["first_message_at"]
        if first_at:
            first_ts = first_at if isinstance(first_at, (int, float)) else 0
            first_seen_days = max(0, int((time.time() - first_ts) / 86400)) if first_ts else 0
        else:
            first_seen_days = 0

        # Determine relationship stage
        if total <= 2:
            stage = "new"
        elif total <= 10 or first_seen_days <= 3:
            stage = "early"
        elif total <= 50 or first_seen_days <= 30:
            stage = "developing"
        else:
            stage = "established"

        return {
            "is_known": is_known,
            "customer_name": customer_name,
            "relationship_stage": stage,
            "total_messages": total,
            "first_seen_days": first_seen_days,
        }
