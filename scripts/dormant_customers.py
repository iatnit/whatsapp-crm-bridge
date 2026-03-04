#!/usr/bin/env python3
"""Dormant customer re-engagement: find inactive qualified/negotiating/sampling
customers and send draft follow-up messages to Feishu for Lucky to review.

Criteria:
  - Last WhatsApp message > 30 days ago (configurable)
  - HubSpot customer_stage in: qualified, negotiating, sampling
  - Has product_interest or demand context

Usage:
    python scripts/dormant_customers.py --dry-run   # preview only
    python scripts/dormant_customers.py             # send drafts to Feishu
    python scripts/dormant_customers.py --days 45   # custom dormant threshold
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# Stages worth re-engaging (customer showed intent but didn't order)
DORMANT_STAGES = {"qualified", "negotiating", "sampling"}


async def _get_dormant_contacts(days: int) -> list[dict]:
    """Find dormant contacts: local SQLite + HubSpot stage filter."""
    from app.store.database import get_db, init_db
    await init_db()

    cutoff_ts = int(time.time()) - days * 86400

    async with get_db() as db:
        cursor = await db.execute(
            """SELECT c.phone, c.customer_name, c.display_name,
                      c.hubspot_contact_id, c.product_interest,
                      c.customer_tier, c.total_messages,
                      MAX(m.timestamp) as last_msg_ts
               FROM conversations c
               LEFT JOIN messages m ON m.phone = c.phone
               WHERE c.hubspot_contact_id != '' AND c.hubspot_contact_id IS NOT NULL
               GROUP BY c.phone
               HAVING last_msg_ts < ? AND last_msg_ts IS NOT NULL
               ORDER BY last_msg_ts DESC""",
            (cutoff_ts,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    logger.info("Found %d conversations inactive for %d+ days", len(rows), days)

    if not rows:
        return []

    # Filter by HubSpot customer_stage
    from app.config import settings
    if not settings.hubspot_access_token:
        logger.error("HUBSPOT_ACCESS_TOKEN not set")
        return []

    import httpx
    headers = {
        "Authorization": f"Bearer {settings.hubspot_access_token}",
        "Content-Type": "application/json",
    }

    contact_ids = [r["hubspot_contact_id"] for r in rows]
    stage_map: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(contact_ids), 100):
            batch = contact_ids[i:i + 100]
            resp = await client.post(
                "https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
                headers=headers,
                json={
                    "inputs": [{"id": cid} for cid in batch],
                    "properties": ["customer_stage", "product_interest"],
                },
            )
            if resp.status_code == 200:
                for result in resp.json().get("results", []):
                    cid = result.get("id")
                    stage = result.get("properties", {}).get("customer_stage", "") or ""
                    stage_map[cid] = stage

    # Filter to dormant stages only
    dormant = []
    for row in rows:
        cid = row["hubspot_contact_id"]
        stage = stage_map.get(cid, "")
        if stage in DORMANT_STAGES:
            row["customer_stage"] = stage
            dormant.append(row)

    logger.info("%d contacts are in dormant stages (%s)", len(dormant), ", ".join(DORMANT_STAGES))
    return dormant


def _build_draft_message(row: dict, days_inactive: int) -> str:
    """Generate a personalized follow-up draft for Lucky to review/send."""
    name = row.get("customer_name") or row.get("display_name") or ""
    first_name = name.split()[0] if name else "there"
    products = (row.get("product_interest") or "").replace(";", ", ")
    stage = row.get("customer_stage", "")

    if stage == "sampling":
        if products:
            return f"Hi {first_name}, just checking — did you get a chance to review the {products} samples we sent? Let me know if you'd like to move forward 👍"
        return f"Hi {first_name}, hope you're doing well! Just wanted to follow up on the samples. Any feedback? 😊"
    elif stage == "negotiating":
        if products:
            return f"Hi {first_name}, hope business is going well! We were discussing {products} — are you still looking for stock? We have good availability now 👍"
        return f"Hi {first_name}, just checking in! Are you still interested in placing an order? Happy to help 😊"
    else:  # qualified
        if products:
            return f"Hi {first_name}, long time no chat! We have new arrivals for {products}, would love to share details if you're still interested 👍"
        return f"Hi {first_name}, hope all is well! Just wanted to reconnect — any new requirements we can help with? 😊"


async def run(days: int, dry_run: bool) -> None:
    dormant = await _get_dormant_contacts(days)

    if not dormant:
        logger.info("No dormant customers found.")
        return

    now_ts = int(time.time())
    logger.info("\n%s Dormant customers to re-engage:", len(dormant))

    feishu_lines: list[str] = []
    feishu_lines.append(f"💤 {len(dormant)} 个沉睡客户需要重新激活（{days}天未联系）\n")

    for row in dormant[:20]:  # cap at 20
        name = row.get("customer_name") or row.get("display_name") or row["phone"]
        phone = row["phone"]
        stage = row.get("customer_stage", "")
        tier = row.get("customer_tier", "")
        days_inactive = (now_ts - (row["last_msg_ts"] or 0)) // 86400
        last_date = datetime.fromtimestamp(row["last_msg_ts"] or 0, tz=CST).strftime("%Y-%m-%d")
        draft = _build_draft_message(row, days_inactive)

        tier_label = f" [{tier}]" if tier else ""
        logger.info("  • %s (%s)%s | stage=%s | inactive=%dd | draft: %s",
                    name, phone, tier_label, stage, days_inactive, draft[:60])

        feishu_lines.append(f"• {name}{tier_label} — 上次联系: {last_date} ({days_inactive}天前)")
        feishu_lines.append(f"  阶段: {stage}")
        feishu_lines.append(f"  草稿: {draft}")
        feishu_lines.append("")

    if len(dormant) > 20:
        feishu_lines.append(f"...还有 {len(dormant) - 20} 个客户未显示")

    if dry_run:
        logger.info("[DRY RUN — not sending to Feishu]")
        return

    # Send to Feishu webhook
    from app.config import settings
    if not settings.feishu_webhook_url:
        logger.warning("FEISHU_WEBHOOK_URL not set, printing only")
        print("\n".join(feishu_lines))
        return

    import httpx
    content_lines = [[{"tag": "text", "text": line}] for line in feishu_lines]
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": f"💤 沉睡客户激活建议 — {datetime.now(CST).strftime('%Y-%m-%d')}",
                    "content": content_lines,
                }
            }
        },
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.feishu_webhook_url, json=payload)

    if resp.status_code == 200 and resp.json().get("code") == 0:
        logger.info("Sent dormant customer report to Feishu ✓")
    else:
        logger.error("Feishu send failed [%d]: %s", resp.status_code, resp.text[:200])


def main() -> None:
    parser = argparse.ArgumentParser(description="Dormant customer re-engagement")
    parser.add_argument("--days", type=int, default=30, help="Days inactive threshold (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no Feishu send")
    args = parser.parse_args()
    asyncio.run(run(days=args.days, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
