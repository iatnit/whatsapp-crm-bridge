"""Conversation-level queries and customer matching updates."""

import time
from datetime import datetime, timedelta, timezone

from app.store.database import get_db


def calc_relationship_stage(total_messages: int, first_seen_days: int) -> str:
    """Determine relationship stage from message count and days since first contact."""
    if total_messages <= 2:
        return "new"
    elif total_messages <= 10 or first_seen_days <= 3:
        return "early"
    elif total_messages <= 50 or first_seen_days <= 30:
        return "developing"
    return "established"


def _parse_first_message_ts(value) -> float:
    """Parse first_message_at into a Unix timestamp.

    Handles ISO strings, SQLite datetime strings, and numeric values.
    Returns 0 if parsing fails.
    """
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # Try ISO format first (e.g. "2026-02-28T15:00:00+00:00")
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                     "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
    return 0


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


async def update_location(phone: str, location: str) -> None:
    """Cache location (country/city) extracted by AI into conversations table."""
    if not location:
        return
    async with get_db() as db:
        await db.execute(
            "UPDATE conversations SET location = ? WHERE phone = ? AND (location = '' OR location IS NULL)",
            (location, phone),
        )
        await db.commit()


async def update_crm_enrichment(
    phone: str,
    tier: str = "",
    product_interest: str = "",
) -> None:
    """Cache HubSpot enrichment data locally for fast AI context lookup.

    Called by the daily pipeline after syncing HubSpot contact properties.
    Only updates non-empty values so partial updates don't wipe existing data.
    """
    async with get_db() as db:
        if tier:
            await db.execute(
                "UPDATE conversations SET customer_tier = ? WHERE phone = ?",
                (tier, phone),
            )
        if product_interest:
            await db.execute(
                "UPDATE conversations SET product_interest = ? WHERE phone = ?",
                (product_interest, phone),
            )
        if tier or product_interest:
            await db.commit()


async def update_customer_stage(phone: str, new_stage: str) -> str:
    """Update customer_stage and return the old stage (for change detection)."""
    if not new_stage:
        return ""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT customer_stage FROM conversations WHERE phone = ?", (phone,)
        )
        row = await cursor.fetchone()
        old_stage = (row["customer_stage"] or "") if row else ""
        if old_stage != new_stage:
            await db.execute(
                "UPDATE conversations SET customer_stage = ? WHERE phone = ?",
                (new_stage, phone),
            )
            await db.commit()
        return old_stage


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


async def update_intent(phone: str, priority: str, tags: str) -> None:
    """Store LLM-extracted intent priority and tags."""
    async with get_db() as db:
        await db.execute(
            "UPDATE conversations SET intent_priority = ?, intent_tags = ? WHERE phone = ?",
            (priority, tags, phone),
        )
        await db.commit()


async def upsert_customer_action(
    phone: str, action_date: str, customer_name: str,
    today_action: str, tomorrow_action: str, pending_customer: str,
    priority: str, summary: str,
) -> None:
    """Insert or replace customer actions for a given date."""
    async with get_db() as db:
        await db.execute(
            """INSERT OR REPLACE INTO customer_actions
               (phone, action_date, customer_name, today_action, tomorrow_action,
                pending_customer, priority, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (phone, action_date, customer_name, today_action,
             tomorrow_action, pending_customer, priority, summary),
        )
        await db.commit()


async def get_pending_actions(action_date: str) -> list[dict]:
    """Get all customer actions for a given date that have non-empty fields."""
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM customer_actions
               WHERE action_date = ?
                 AND (today_action != '' OR pending_customer != '')
               ORDER BY
                 CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                 customer_name""",
            (action_date,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_yesterday_tomorrow_actions(yesterday: str) -> list[dict]:
    """Get yesterday's 'tomorrow' actions (they become today's tasks)."""
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM customer_actions
               WHERE action_date = ? AND tomorrow_action != ''
               ORDER BY customer_name""",
            (yesterday,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_customer_context(phone: str) -> dict:
    """Build customer context from local SQLite data (zero API calls).

    Returns a dict with:
      is_known, customer_name, relationship_stage, total_messages, first_seen_days,
      customer_tier, product_interest
    """
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT customer_name, match_status, total_messages, first_message_at, "
            "customer_tier, product_interest "
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
                "customer_tier": "",
                "product_interest": "",
            }

        total = conv["total_messages"] or 0
        customer_name = conv["customer_name"] or ""
        is_known = conv["match_status"] == "matched" and bool(customer_name)

        first_ts = _parse_first_message_ts(conv["first_message_at"])
        first_seen_days = max(0, int((time.time() - first_ts) / 86400)) if first_ts else 0

        stage = calc_relationship_stage(total, first_seen_days)

        return {
            "is_known": is_known,
            "customer_name": customer_name,
            "relationship_stage": stage,
            "total_messages": total,
            "first_seen_days": first_seen_days,
            "customer_tier": conv["customer_tier"] or "",
            "product_interest": conv["product_interest"] or "",
        }


async def get_overview_stats() -> dict:
    """Return aggregated CRM stats for reports and dashboard (SQLite only, no API calls).

    Includes: total customers, active/new last 7 days, hot leads today,
    tier distribution, priority distribution, 7-day message volume, top 10 customers.
    """
    cst = timezone(timedelta(hours=8))
    now = datetime.now(cst)
    ts_7d_ago = int((now - timedelta(days=7)).timestamp())
    today_str = now.strftime("%Y-%m-%d")
    week_start_str = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM conversations")
        total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(DISTINCT phone) FROM messages WHERE timestamp >= ?",
            (ts_7d_ago,),
        )
        active_7d = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM conversations WHERE first_message_at >= ?",
            (week_start_str,),
        )
        new_7d = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM customer_actions WHERE action_date = ? AND priority = 'high'",
            (today_str,),
        )
        hot_leads = (await cursor.fetchone())[0]

        # Tier distribution
        cursor = await db.execute(
            "SELECT COALESCE(customer_tier, '') as tier, COUNT(*) as cnt "
            "FROM conversations GROUP BY tier ORDER BY tier"
        )
        tier_rows = await cursor.fetchall()
        tiers = [{"tier": r[0] or "未设置", "count": r[1]} for r in tier_rows]

        # Priority distribution from each customer's most recent action
        cursor = await db.execute("""
            SELECT ca.priority, COUNT(*)
            FROM customer_actions ca
            WHERE ca.action_date = (
                SELECT MAX(action_date) FROM customer_actions WHERE phone = ca.phone
            )
            GROUP BY ca.priority
        """)
        prio_rows = await cursor.fetchall()
        priorities = [{"priority": r[0] or "normal", "count": r[1]} for r in prio_rows]

        # 7-day message volume by day
        msg_7d = []
        for i in range(6, -1, -1):
            day = now - timedelta(days=i)
            day_start = int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            day_end = int(day.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())
            cursor = await db.execute(
                "SELECT COUNT(*), direction FROM messages "
                "WHERE timestamp >= ? AND timestamp <= ? GROUP BY direction",
                (day_start, day_end),
            )
            rows = await cursor.fetchall()
            msg_7d.append({
                "date": day.strftime("%m/%d"),
                "inbound": next((r[0] for r in rows if r[1] == "inbound"), 0),
                "outbound": next((r[0] for r in rows if r[1] == "outbound"), 0),
            })

        # Top 30 customers by total_messages
        cursor = await db.execute(
            "SELECT phone, display_name, total_messages, customer_tier, last_message_at, location "
            "FROM conversations ORDER BY total_messages DESC LIMIT 30"
        )
        top_rows = await cursor.fetchall()
        top_customers = [
            {
                "phone": r[0],
                "phone_short": r[0][-5:] if r[0] else "",
                "name": r[1] or r[0],
                "msgs": r[2] or 0,
                "tier": r[3] or "",
                "last_contact": r[4][:10] if r[4] else "",
                "city": r[5] or "",
            }
            for r in top_rows
        ]

    return {
        "total_customers": total,
        "active_7d": active_7d,
        "new_7d": new_7d,
        "hot_leads": hot_leads,
        "tiers": tiers,
        "priorities": priorities,
        "msg_7d": msg_7d,
        "top_customers": top_customers,
    }
