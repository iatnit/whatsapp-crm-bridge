"""Daily follow-up reminder: send morning WhatsApp summary to Lucky."""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings

logger = logging.getLogger(__name__)


async def send_daily_reminder() -> bool:
    """Build and send the morning follow-up reminder via WhatsApp.

    Collects:
    1. Today's actions from pipeline analysis (today_action + pending_customer)
    2. Yesterday's "tomorrow" actions (carried over)
    3. Hot leads (priority=high)

    Sends formatted message to settings.notify_phone.
    Returns True if sent successfully.
    """
    if not settings.notify_phone:
        logger.info("Daily reminder skipped: NOTIFY_PHONE not configured")
        return False

    from app.store.conversations import get_pending_actions, get_yesterday_tomorrow_actions
    from app.webhook.sender import send_text_message

    cst = timezone(timedelta(hours=8))
    today = datetime.now(cst)
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # Fetch data
    today_actions = await get_pending_actions(today_str)
    yesterday_carry = await get_yesterday_tomorrow_actions(yesterday_str)

    # Build carry-over set (yesterday's "tomorrow" = today's tasks)
    carry_phones = set()
    carry_items = []
    for a in yesterday_carry:
        phone = a["phone"]
        if phone not in carry_phones:
            carry_phones.add(phone)
            carry_items.append(a)

    if not today_actions and not carry_items:
        logger.info("Daily reminder: no pending actions for %s", today_str)
        return False

    # Format message
    lines = [f"📋 *今日跟进提醒* ({today_str})", ""]

    # Hot leads first
    hot = [a for a in today_actions if a["priority"] == "high"]
    if hot:
        lines.append("🔥 *热线索*")
        for a in hot:
            name = a["customer_name"] or a["phone"]
            lines.append(f"  • *{name}*: {a['summary']}")
            if a["today_action"]:
                lines.append(f"    → 今天: {a['today_action']}")
            if a["pending_customer"]:
                lines.append(f"    ⏳ 等客户: {a['pending_customer']}")
        lines.append("")

    # Today's actions (non-hot)
    normal = [a for a in today_actions if a["priority"] != "high"]
    if normal:
        lines.append("📌 *今日待办*")
        for a in normal:
            name = a["customer_name"] or a["phone"]
            parts = []
            if a["today_action"]:
                parts.append(a["today_action"])
            if a["pending_customer"]:
                parts.append(f"⏳{a['pending_customer']}")
            action_text = " | ".join(parts) if parts else a["summary"]
            lines.append(f"  • {name}: {action_text}")
        lines.append("")

    # Carry-over from yesterday
    carry_new = [c for c in carry_items if c["phone"] not in {a["phone"] for a in today_actions}]
    if carry_new:
        lines.append("📎 *昨日延续*")
        for a in carry_new:
            name = a["customer_name"] or a["phone"]
            lines.append(f"  • {name}: {a['tomorrow_action']}")
        lines.append("")

    # Stats
    total = len(today_actions) + len(carry_new)
    hot_count = len(hot)
    lines.append(f"共 {total} 个客户待跟进" + (f"，其中 {hot_count} 个热线索" if hot_count else ""))

    message = "\n".join(lines)

    # Send via WhatsApp
    wa_id = await send_text_message(settings.notify_phone, message)
    if wa_id:
        logger.info("Daily reminder sent to %s (%d chars)", settings.notify_phone, len(message))
        return True
    else:
        logger.error("Failed to send daily reminder to %s", settings.notify_phone)
        return False
