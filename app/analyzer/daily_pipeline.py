"""Daily analysis pipeline: aggregate → analyze → write to Feishu."""

import logging
from datetime import datetime, timedelta, timezone
from itertools import groupby
from operator import itemgetter

from app.store.messages import get_unprocessed_messages, mark_processed
from app.store.conversations import get_all_conversations
from app.matcher.customer_matcher import match_all_unmatched, load_customers
from app.analyzer.claude_analyzer import analyze_conversation
from app.writers.feishu_writer import ensure_customer, ensure_followup, clear_customer_cache

logger = logging.getLogger(__name__)


async def run_daily_pipeline() -> dict:
    """Main daily analysis entry point.

    1. Load customers & run matching
    2. Fetch unprocessed messages
    3. Group by phone
    4. Analyze each conversation via Claude
    5. Write results to Feishu
    6. Mark messages as processed

    Returns a summary dict for the daily report.
    """
    logger.info("=== Daily pipeline started ===")

    # Clear customer cache to ensure fresh lookups each run
    clear_customer_cache()

    # Step 1: Refresh customer DB and match unmatched conversations
    load_customers()
    match_results = await match_all_unmatched()
    logger.info("Matching pass: %d new matches", len(match_results))

    # Step 2: Get unprocessed messages (default: all unprocessed)
    messages = await get_unprocessed_messages()
    if not messages:
        logger.info("No unprocessed messages. Pipeline done.")
        return {"total_conversations": 0, "analyzed": 0, "written": 0, "errors": []}

    logger.info("Found %d unprocessed messages", len(messages))

    # Step 3: Group by phone
    messages.sort(key=itemgetter("phone"))
    grouped = {
        phone: list(msgs)
        for phone, msgs in groupby(messages, key=itemgetter("phone"))
    }
    logger.info("Grouped into %d conversations", len(grouped))

    # Step 4 & 5: Analyze each conversation and write to Feishu
    conversations_db = {c["phone"]: c for c in await get_all_conversations()}

    analyzed_count = 0
    written_count = 0
    errors: list[str] = []
    results: list[dict] = []

    for phone, msgs in grouped.items():
        conv = conversations_db.get(phone, {})
        display_name = conv.get("display_name", "") or msgs[0].get("display_name", "")
        customer_name = conv.get("customer_name", "") or display_name or phone

        logger.info("Analyzing %s (%s) — %d messages", customer_name, phone, len(msgs))

        # Analyze
        analysis = await analyze_conversation(msgs, customer_name, phone)
        if not analysis:
            errors.append(f"Analysis failed for {customer_name} ({phone})")
            continue
        analyzed_count += 1

        # Write to Feishu
        try:
            # Determine customer name for Feishu
            # Priority: matched CRM name > Claude analysis name > display_name > phone
            matched_name = conv.get("customer_name", "")
            claude_name = analysis.get("customer_info", {}).get("name", "")
            feishu_name = matched_name or claude_name or display_name or phone

            location = analysis.get("customer_info", {}).get("location", "")

            # Ensure customer exists in Feishu CRM
            record_id = await ensure_customer(feishu_name, phone=phone, location=location)
            if not record_id:
                errors.append(f"Failed to create/find Feishu customer for {feishu_name}")
                continue

            # Create or update follow-up record (max 1 per customer per day)
            followup_id = await ensure_followup(
                customer_record_id=record_id,
                customer_name=feishu_name,
                title=analysis.get("followup_title", "WhatsApp沟通"),
                detail=analysis.get("followup_detail", ""),
                summary=analysis.get("summary", ""),
                method="WhatsApp沟通",
            )
            if followup_id:
                written_count += 1
                logger.info("Feishu followup created: %s for %s", followup_id, feishu_name)
            else:
                errors.append(f"Failed to create followup for {feishu_name}")

        except Exception as e:
            logger.error("Feishu write error for %s: %s", customer_name, e)
            errors.append(f"Feishu error for {customer_name}: {e}")

        results.append({
            "phone": phone,
            "customer_name": customer_name,
            "analysis": analysis,
            "feishu_written": written_count > 0,
        })

        # Mark these messages as processed
        msg_ids = [m["id"] for m in msgs]
        await mark_processed(msg_ids)

    summary = {
        "total_conversations": len(grouped),
        "total_messages": len(messages),
        "analyzed": analyzed_count,
        "written": written_count,
        "new_matches": len(match_results),
        "errors": errors,
        "results": results,
    }

    logger.info(
        "=== Pipeline done: %d conversations, %d analyzed, %d written, %d errors ===",
        len(grouped), analyzed_count, written_count, len(errors),
    )
    return summary
