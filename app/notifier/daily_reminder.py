"""Daily follow-up reminder: send morning summary via Feishu webhook."""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def _send_feishu_webhook(title: str, content_lines: list[list[dict]]) -> bool:
    """Send a rich-text message via Feishu group bot webhook.

    Args:
        title: Card title.
        content_lines: Feishu rich-text content (list of line elements).

    Returns True if sent successfully.
    """
    if not settings.feishu_webhook_url:
        logger.info("Feishu webhook not configured")
        return False

    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content_lines,
                }
            }
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(settings.feishu_webhook_url, json=payload)
        if resp.status_code == 200 and resp.json().get("code") == 0:
            logger.info("Feishu webhook sent: %s", title)
            return True
        logger.error("Feishu webhook failed [%d]: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Feishu webhook error: %s", e)
    return False


def _build_reminder_content(
    today_actions: list[dict],
    carry_items: list[dict],
) -> list[list[dict]]:
    """Build Feishu rich-text content lines from action data."""
    lines: list[list[dict]] = []

    # Hot leads
    hot = [a for a in today_actions if a["priority"] == "high"]
    if hot:
        lines.append([{"tag": "text", "text": "🔥 热线索\n"}])
        for a in hot:
            name = a["customer_name"] or a["phone"]
            text = f"  • {name}: {a['summary']}"
            lines.append([{"tag": "text", "text": text}])
            if a["today_action"]:
                lines.append([{"tag": "text", "text": f"    → 今天: {a['today_action']}"}])
            if a["pending_customer"]:
                lines.append([{"tag": "text", "text": f"    ⏳ 等客户: {a['pending_customer']}"}])
        lines.append([{"tag": "text", "text": ""}])

    # Normal actions
    normal = [a for a in today_actions if a["priority"] != "high"]
    if normal:
        lines.append([{"tag": "text", "text": "📌 今日待办\n"}])
        for a in normal:
            name = a["customer_name"] or a["phone"]
            parts = []
            if a["today_action"]:
                parts.append(a["today_action"])
            if a["pending_customer"]:
                parts.append(f"⏳{a['pending_customer']}")
            action_text = " | ".join(parts) if parts else a["summary"]
            lines.append([{"tag": "text", "text": f"  • {name}: {action_text}"}])
        lines.append([{"tag": "text", "text": ""}])

    # Carry-over from yesterday
    today_phones = {a["phone"] for a in today_actions}
    carry_new = [c for c in carry_items if c["phone"] not in today_phones]
    if carry_new:
        lines.append([{"tag": "text", "text": "📎 昨日延续\n"}])
        for a in carry_new:
            name = a["customer_name"] or a["phone"]
            lines.append([{"tag": "text", "text": f"  • {name}: {a['tomorrow_action']}"}])
        lines.append([{"tag": "text", "text": ""}])

    # Stats
    total = len(today_actions) + len(carry_new)
    hot_count = len(hot)
    stat = f"共 {total} 个客户待跟进"
    if hot_count:
        stat += f"，其中 {hot_count} 个热线索"
    lines.append([{"tag": "text", "text": stat}])

    return lines


def _build_sync_content(sync: dict) -> list[list[dict]]:
    """Build Feishu content lines for data sync health report."""
    total = sync.get("total_conversations", 0)
    both = sync.get("both_linked", 0)
    feishu_only = sync.get("feishu_only", 0)
    hs_only = sync.get("hubspot_only", 0)
    neither = sync.get("neither", 0)

    if neither == 0 and feishu_only == 0 and hs_only == 0:
        return []  # All healthy, skip section

    lines: list[list[dict]] = []
    lines.append([{"tag": "text", "text": ""}])
    lines.append([{"tag": "text", "text": "🔍 数据巡检\n"}])
    lines.append([{"tag": "text", "text": f"  总客户: {total} | 双系统: {both} | 飞书独有: {feishu_only} | HS独有: {hs_only}"}])
    if neither:
        lines.append([{"tag": "text", "text": f"  ⚠️ {neither} 个客户未关联任何CRM"}])
    return lines


async def send_daily_reminder() -> bool:
    """Build and send the morning follow-up reminder via Feishu webhook.

    Collects:
    1. Today's actions from pipeline analysis (today_action + pending_customer)
    2. Yesterday's "tomorrow" actions (carried over)
    3. Hot leads (priority=high)

    Returns True if sent successfully.
    """
    if not settings.feishu_webhook_url:
        logger.info("Daily reminder skipped: FEISHU_WEBHOOK_URL not configured")
        return False

    from app.store.conversations import get_pending_actions, get_yesterday_tomorrow_actions

    cst = timezone(timedelta(hours=8))
    today = datetime.now(cst)
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # Fetch data
    today_actions = await get_pending_actions(today_str)
    yesterday_carry = await get_yesterday_tomorrow_actions(yesterday_str)

    # Dedup carry-over
    carry_phones = set()
    carry_items = []
    for a in yesterday_carry:
        phone = a["phone"]
        if phone not in carry_phones:
            carry_phones.add(phone)
            carry_items.append(a)

    # Fetch sync status (Feishu ↔ HubSpot data health)
    from app.store.conversations import get_sync_status
    sync = await get_sync_status()

    if not today_actions and not carry_items:
        logger.info("Daily reminder: no pending actions for %s", today_str)
        # Still send sync report if there are gaps
        if sync.get("neither", 0) > 0:
            title = f"📋 数据巡检 — {today_str}"
            content = _build_sync_content(sync)
            return await _send_feishu_webhook(title, content)
        return False

    # Build and send
    title = f"📋 今日跟进提醒 — {today_str}"
    content = _build_reminder_content(today_actions, carry_items)
    content.extend(_build_sync_content(sync))
    return await _send_feishu_webhook(title, content)
