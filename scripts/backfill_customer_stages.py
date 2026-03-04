#!/usr/bin/env python3
"""Backfill customer_stage for HubSpot contacts that have no stage set.

Uses local SQLite data (message count, product_interest) to infer stage
without LLM calls. Respects the anti-regression logic in hubspot_writer
(never downgrades an existing stage).

Usage:
    python scripts/backfill_customer_stages.py --dry-run   # preview only
    python scripts/backfill_customer_stages.py             # apply changes
    python scripts/backfill_customer_stages.py --all       # overwrite even existing stages
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# Stage priority (mirrors hubspot_writer._STAGE_ORDER)
_STAGE_ORDER = {
    "new_lead": 1, "contacted": 2, "qualified": 3,
    "negotiating": 4, "sampling": 5, "ordered": 6,
    "repeat_buyer": 7, "dormant": 0, "lost": 0,
}


def _infer_stage(total_messages: int, product_interest: str, intent_tags: str) -> str:
    """Infer customer_stage from local SQLite data (no LLM needed).

    Stage logic (highest applicable wins):
    - repeat_buyer: 3+ separate order signals in intent_tags
    - ordered:      intent_tags contains 'ordered'
    - sampling:     intent_tags contains 'sample'
    - negotiating:  product_interest set AND messages > 15
    - qualified:    product_interest set OR messages > 8
    - contacted:    messages > 2
    - new_lead:     fallback
    """
    stage = "new_lead"

    if total_messages > 2:
        stage = "contacted"

    if product_interest or total_messages > 8:
        stage = "qualified"

    if product_interest and total_messages > 15:
        stage = "negotiating"

    tags_lower = (intent_tags or "").lower()
    if "sample" in tags_lower:
        stage = "sampling"

    if "ordered" in tags_lower or "order" in tags_lower:
        stage = "ordered"

    if tags_lower.count("order") >= 3 or "repeat" in tags_lower:
        stage = "repeat_buyer"

    return stage


async def run(dry_run: bool, overwrite_all: bool) -> None:
    from app.config import settings
    from app.store.database import get_db, init_db
    import httpx

    await init_db()  # ensure migrations are applied

    if not settings.hubspot_access_token:
        logger.error("HUBSPOT_ACCESS_TOKEN not set")
        return

    headers = {
        "Authorization": f"Bearer {settings.hubspot_access_token}",
        "Content-Type": "application/json",
    }
    base_url = "https://api.hubapi.com"

    # 1. Load all conversations with hubspot_contact_id from SQLite
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT phone, hubspot_contact_id, total_messages,
                      product_interest, intent_tags
               FROM conversations
               WHERE hubspot_contact_id != '' AND hubspot_contact_id IS NOT NULL
               ORDER BY total_messages DESC"""
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    logger.info("Found %d conversations with HubSpot contact IDs", len(rows))

    # 2. Batch-fetch current customer_stage from HubSpot (100 at a time)
    contact_ids = [r["hubspot_contact_id"] for r in rows]
    current_stages: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(contact_ids), 100):
            batch = contact_ids[i:i + 100]
            resp = await client.post(
                f"{base_url}/crm/v3/objects/contacts/batch/read",
                headers=headers,
                json={
                    "inputs": [{"id": cid} for cid in batch],
                    "properties": ["customer_stage"],
                },
            )
            if resp.status_code != 200:
                logger.error("Batch read failed [%d]: %s", resp.status_code, resp.text[:200])
                continue
            for result in resp.json().get("results", []):
                cid = result.get("id")
                stage = result.get("properties", {}).get("customer_stage", "") or ""
                current_stages[cid] = stage

    logger.info("Fetched current stages for %d contacts", len(current_stages))

    # 3. Determine updates needed
    updates: list[dict] = []
    skipped_already_set = 0
    skipped_higher = 0

    for row in rows:
        cid = row["hubspot_contact_id"]
        current = current_stages.get(cid, "")
        inferred = _infer_stage(
            row["total_messages"] or 0,
            row["product_interest"] or "",
            row["intent_tags"] or "",
        )

        if current and not overwrite_all:
            # Skip contacts that already have a stage (unless --all)
            skipped_already_set += 1
            continue

        if current and _STAGE_ORDER.get(current, 0) >= _STAGE_ORDER.get(inferred, 0):
            # Never downgrade
            skipped_higher += 1
            continue

        updates.append({
            "contact_id": cid,
            "phone": row["phone"],
            "current_stage": current,
            "new_stage": inferred,
            "msgs": row["total_messages"],
        })

    logger.info(
        "To update: %d | Already set (skip): %d | Higher stage (no downgrade): %d",
        len(updates), skipped_already_set, skipped_higher,
    )

    if not updates:
        logger.info("Nothing to do.")
        return

    # Show preview
    stage_counts: dict[str, int] = {}
    for u in updates:
        stage_counts[u["new_stage"]] = stage_counts.get(u["new_stage"], 0) + 1
    for stage, count in sorted(stage_counts.items()):
        logger.info("  → %-15s : %d contacts", stage, count)

    if dry_run:
        logger.info("[DRY RUN — no changes made]")
        return

    # 4. Apply updates in batches of 100
    async with httpx.AsyncClient(timeout=30) as client:
        applied = 0
        errors = 0
        for i in range(0, len(updates), 100):
            batch = updates[i:i + 100]
            resp = await client.post(
                f"{base_url}/crm/v3/objects/contacts/batch/update",
                headers=headers,
                json={
                    "inputs": [
                        {"id": u["contact_id"], "properties": {"customer_stage": u["new_stage"]}}
                        for u in batch
                    ]
                },
            )
            if resp.status_code in (200, 207):
                batch_results = resp.json().get("results", batch)
                applied += len(batch_results)
                logger.info("Batch %d/%d: %d updated", i // 100 + 1,
                            (len(updates) + 99) // 100, len(batch_results))
            else:
                errors += len(batch)
                logger.error("Batch update failed [%d]: %s", resp.status_code, resp.text[:200])

    logger.info("Done: %d updated, %d errors", applied, errors)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill customer_stage in HubSpot")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    parser.add_argument("--all", dest="overwrite_all", action="store_true",
                        help="Overwrite even existing stages (still respects anti-regression)")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, overwrite_all=args.overwrite_all))


if __name__ == "__main__":
    main()
