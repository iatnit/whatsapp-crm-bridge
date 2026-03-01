"""Daily analysis pipeline: aggregate → analyze → write to Feishu & HubSpot."""

import json
import logging
from datetime import datetime, timedelta, timezone
from itertools import groupby
from operator import itemgetter
from pathlib import Path

from app.store.messages import get_unprocessed_messages, mark_processed
from app.store.conversations import (
    get_all_conversations,
    get_unmatched_conversations,
    update_customer_match,
    update_hubspot_id,
)
from app.store.retry_queue import enqueue, get_pending, mark_success, mark_retried, cleanup_old
from app.matcher.customer_matcher import match_all_unmatched, load_customers
from app.analyzer.claude_analyzer import analyze_conversation
from app.config import settings
from app.writers.feishu_writer import (
    ensure_customer, ensure_followup, clear_customer_cache, get_customer_number,
)
from app.writers.hubspot_writer import (
    ensure_contact as hubspot_ensure_contact,
    ensure_note as hubspot_ensure_note,
    ensure_deal as hubspot_ensure_deal,
    clear_contact_cache as hubspot_clear_cache,
    build_hubspot_properties,
)

logger = logging.getLogger(__name__)


async def _auto_create_unmatched(
    already_processed: dict[str, dict],
    errors: list[str],
) -> int:
    """Auto-create Feishu customer records for unmatched conversations.

    Handles the backlog of conversations that were never matched and whose
    messages were already processed in prior runs. Queries the DB for
    conversations still marked as 'unmatched' (pipeline loop already updated
    any it processed to 'auto_created').

    Returns count of newly auto-created customers.
    """
    unmatched = await get_unmatched_conversations()
    if not unmatched:
        return 0

    logger.info("Processing %d unmatched conversations (backlog)", len(unmatched))
    count = 0

    for conv in unmatched:
        phone = conv["phone"]
        display_name = conv.get("display_name", "") or phone

        try:
            record_id = await ensure_customer(
                display_name, phone=phone, contact_person=display_name,
            )
            if record_id:
                await update_customer_match(phone, record_id, display_name, "auto_created")
                count += 1
                logger.info("Backlog auto-created customer '%s' for %s", display_name, phone)
            else:
                errors.append(f"Failed to auto-create customer for {display_name} ({phone})")
        except Exception as e:
            logger.error("Auto-create error for %s: %s", phone, e)
            errors.append(f"Auto-create error for {display_name}: {e}")

    logger.info("Auto-created %d / %d unmatched backlog customers", count, len(unmatched))
    return count


async def _process_retry_queue() -> int:
    """Retry previously failed CRM writes. Returns count of recovered items."""
    pending = await get_pending()
    if not pending:
        return 0

    logger.info("Retrying %d failed writes", len(pending))
    recovered = 0

    for item in pending:
        item_id = item["id"]
        target = item["target"]
        operation = item["operation"]
        args = json.loads(item["args_json"])

        try:
            if target == "feishu" and operation == "ensure_customer":
                result = await ensure_customer(**args)
                if result:
                    await mark_success(item_id)
                    recovered += 1
                    logger.info("Retry OK: feishu.ensure_customer(%s)", args.get("name"))
                else:
                    await mark_retried(item_id, "returned None")
            elif target == "hubspot" and operation == "ensure_contact":
                result = await hubspot_ensure_contact(**args)
                if result:
                    await mark_success(item_id)
                    recovered += 1
                    logger.info("Retry OK: hubspot.ensure_contact(%s)", args.get("phone"))
                else:
                    await mark_retried(item_id, "returned None")
            else:
                logger.warning("Unknown retry target: %s.%s", target, operation)
                await mark_retried(item_id, f"unknown operation: {target}.{operation}")
        except Exception as e:
            logger.error("Retry failed for %s.%s: %s", target, operation, e)
            await mark_retried(item_id, str(e))

    return recovered


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

    # Clear caches to ensure fresh lookups each run
    clear_customer_cache()
    hubspot_clear_cache()

    # Step 0: Retry previously failed writes
    retry_count = await _process_retry_queue()
    if retry_count:
        logger.info("Retry queue: %d items recovered", retry_count)

    # Clean up old retry records (>7 days)
    cleaned = await cleanup_old(days=7)
    if cleaned:
        logger.info("Cleaned %d old retry records", cleaned)

    # Step 1: Refresh customer DB and match unmatched conversations
    load_customers()
    match_results = await match_all_unmatched()
    logger.info("Matching pass: %d new matches", len(match_results))

    # Step 2: Get unprocessed messages (default: all unprocessed)
    messages = await get_unprocessed_messages()
    if not messages:
        logger.info("No unprocessed messages, checking unmatched backlog only")
        backlog_errors: list[str] = []
        auto_created = await _auto_create_unmatched({}, backlog_errors)
        return {
            "total_conversations": 0, "analyzed": 0, "written": 0,
            "auto_created": auto_created, "errors": backlog_errors,
        }

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

        # Collect image file paths for this customer
        image_paths = [
            m["media_path"] for m in msgs
            if m.get("media_path")
            and m.get("msg_type") == "image"
            and Path(m["media_path"]).exists()
        ]
        if image_paths:
            logger.info("Found %d images for %s", len(image_paths), customer_name)

        # Analyze
        analysis = await analyze_conversation(msgs, customer_name, phone)
        if not analysis:
            errors.append(f"Analysis failed for {customer_name} ({phone})")
            continue
        analyzed_count += 1

        # Determine customer name (shared by Feishu + HubSpot)
        # Priority: matched CRM name > Claude analysis name > display_name > phone
        matched_name = conv.get("customer_name", "")
        claude_name = analysis.get("customer_info", {}).get("name", "")
        feishu_name = matched_name or claude_name or display_name or phone
        location = analysis.get("customer_info", {}).get("location", "")

        # Write to Feishu
        feishu_ok = False
        record_id = None
        try:
            record_id = await ensure_customer(
                feishu_name, phone=phone, location=location,
                contact_person=display_name,
            )
            if not record_id:
                errors.append(f"Failed to create/find Feishu customer for {feishu_name}")
                await enqueue("feishu", "ensure_customer", {
                    "name": feishu_name, "phone": phone,
                    "location": location, "contact_person": display_name,
                }, "record_id was None")
            else:
                # Update match_status if conversation was unmatched
                match_status = conv.get("match_status", "")
                if match_status in ("unmatched", "", None):
                    await update_customer_match(
                        phone, record_id, feishu_name, "auto_created"
                    )
                    logger.info("Auto-created customer '%s' for %s", feishu_name, phone)
                followup_id = await ensure_followup(
                    customer_record_id=record_id,
                    customer_name=feishu_name,
                    title=analysis.get("followup_title", "WhatsApp沟通"),
                    detail=analysis.get("followup_detail", ""),
                    summary=analysis.get("summary", ""),
                    method="WhatsApp沟通",
                    image_paths=image_paths,
                )
                if followup_id:
                    written_count += 1
                    feishu_ok = True
                    logger.info("Feishu followup created: %s for %s", followup_id, feishu_name)
                else:
                    errors.append(f"Failed to create followup for {feishu_name}")
        except Exception as e:
            logger.error("Feishu write error for %s: %s", customer_name, e)
            errors.append(f"Feishu error for {customer_name}: {e}")
            await enqueue("feishu", "ensure_customer", {
                "name": feishu_name, "phone": phone,
                "location": location, "contact_person": display_name,
            }, str(e))

        # HubSpot 写入（独立于飞书，互不阻塞）
        hubspot_written = False
        hs_contact_id = None
        if settings.hubspot_enabled:
            try:
                total_msgs = conv.get("total_messages", 0) or len(msgs)
                hs_extra = build_hubspot_properties(analysis, phone, total_messages=total_msgs)

                # P3: Sync Feishu 编号 → HubSpot feishu_customer_id
                if record_id:
                    feishu_number = get_customer_number(record_id)
                    if feishu_number:
                        hs_extra["feishu_customer_id"] = feishu_number

                hs_contact_id = await hubspot_ensure_contact(
                    phone, name=feishu_name, country=location, extra=hs_extra)
                if hs_contact_id:
                    # P3: Store HubSpot contact ID in conversations table
                    await update_hubspot_id(phone, hs_contact_id)

                    hs_note_id = await hubspot_ensure_note(
                        hs_contact_id, phone,
                        title=analysis.get("followup_title", "WhatsApp沟通"),
                        detail=analysis.get("followup_detail", ""),
                        summary=analysis.get("summary", ""))
                    hubspot_written = bool(hs_note_id)
            except Exception as e:
                logger.error("HubSpot error for %s: %s", customer_name, e)
                await enqueue("hubspot", "ensure_contact", {
                    "phone": phone, "name": feishu_name, "country": location,
                }, str(e))

        # HubSpot Deal 创建（当LLM检测到确认订单时）
        order_info = analysis.get("order_info", {})
        if order_info.get("order_confirmed") and settings.hubspot_enabled and hs_contact_id:
            try:
                order_desc = order_info.get("order_description", "")
                order_products = order_info.get("order_products", [])
                deal_name = f"{feishu_name} - {order_desc or ', '.join(order_products) or 'WhatsApp订单'}"
                deal_id = await hubspot_ensure_deal(
                    contact_id=hs_contact_id,
                    deal_name=deal_name,
                    stage="closedwon",
                )
                if deal_id:
                    logger.info("HubSpot deal created: %s for %s", deal_id, feishu_name)
                    # Also update customer stage to ordered
                    from app.writers.hubspot_writer import update_contact
                    await update_contact(hs_contact_id, extra={"customer_stage": "ordered"})
            except Exception as e:
                logger.error("HubSpot deal creation error for %s: %s", customer_name, e)

        results.append({
            "phone": phone,
            "customer_name": customer_name,
            "analysis": analysis,
            "feishu_written": feishu_ok,
            "hubspot_written": hubspot_written,
        })

        # Mark processed only if at least one CRM write succeeded
        if feishu_ok or hubspot_written:
            msg_ids = [m["id"] for m in msgs]
            await mark_processed(msg_ids)
        else:
            logger.warning("Skipping mark_processed for %s — no CRM write succeeded", customer_name)

    # Step 6: Auto-create customers for remaining unmatched conversations
    # (backlog — those with no unprocessed messages left)
    auto_created = await _auto_create_unmatched(conversations_db, errors)

    summary = {
        "total_conversations": len(grouped),
        "total_messages": len(messages),
        "analyzed": analyzed_count,
        "written": written_count,
        "new_matches": len(match_results),
        "auto_created": auto_created,
        "retried": retry_count,
        "errors": errors,
        "results": results,
    }

    logger.info(
        "=== Pipeline done: %d conversations, %d analyzed, %d written, %d errors ===",
        len(grouped), analyzed_count, written_count, len(errors),
    )
    return summary
