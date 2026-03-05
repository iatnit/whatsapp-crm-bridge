"""Manual trigger endpoints for pipeline, sync, reminders, and reports."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.auth import verify_admin
from app.utils.tasks import safe_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["triggers"])
limiter = Limiter(key_func=get_remote_address)


@router.post("/analyze/trigger", dependencies=[Depends(verify_admin)])
@limiter.limit("5/minute")
async def manual_trigger(request: Request):
    """Manually trigger the daily analysis pipeline (for testing)."""
    from app.analyzer.daily_pipeline import run_daily_pipeline
    from app.store.conversations import get_unmatched_conversations, get_overview_stats
    from app.writers.report_writer import generate_daily_report, write_report_to_feishu, write_report_to_notion

    summary = await run_daily_pipeline()
    unmatched = await get_unmatched_conversations()
    overview = await get_overview_stats()
    report = generate_daily_report(summary, unmatched=unmatched, overview=overview)
    try:
        await write_report_to_feishu(report, summary)
    except Exception as e:
        logger.warning("Manual trigger Feishu write failed: %s", e)
    try:
        await write_report_to_notion(report, summary)
    except Exception as e:
        logger.warning("Manual trigger Notion write failed: %s", e)
    return {"summary": summary, "report": report}


@router.post("/feishu-hs-sync/trigger", dependencies=[Depends(verify_admin)])
@limiter.limit("5/minute")
async def manual_feishu_hs_sync(request: Request):
    """Manually trigger Feishu 跟进记录 → HubSpot Notes sync (runs in background)."""
    from app.sync.feishu_to_hubspot import sync_feishu_to_hubspot
    safe_task(sync_feishu_to_hubspot(), name="feishu-hs-sync")
    return {"status": "started", "message": "Sync running in background, check logs for progress"}


@router.post("/reminder/trigger", dependencies=[Depends(verify_admin)])
@limiter.limit("5/minute")
async def manual_reminder(request: Request):
    """Manually trigger the daily follow-up reminder (for testing)."""
    from app.notifier.daily_reminder import send_daily_reminder
    sent = await send_daily_reminder()
    return {"sent": sent}


@router.post("/dormant/trigger", dependencies=[Depends(verify_admin)])
@limiter.limit("5/minute")
async def manual_dormant_outreach(request: Request, days: int = 30):
    """Manually trigger dormant customer outreach report to Feishu."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from scripts.dormant_customers import run as dormant_run
    safe_task(dormant_run(days=days, dry_run=False), name="dormant-outreach")
    return {"status": "started", "days": days}


@router.post("/weekly-ceo-report/trigger", dependencies=[Depends(verify_admin)])
@limiter.limit("5/minute")
async def manual_weekly_ceo_report(request: Request, days: int = 7):
    """Manually trigger CEO weekly report generation."""
    from app.notifier.weekly_ceo_report import run_weekly_ceo_report
    safe_task(run_weekly_ceo_report(days=days), name="weekly-ceo-report")
    return {"status": "started", "days": days}


@router.post("/send", dependencies=[Depends(verify_admin)])
@limiter.limit("30/minute")
async def send_message(request: Request, payload: dict):
    """Send a WhatsApp message and record it in the database.

    Body: {"to": "919876543210", "text": "Hello!"}
    """
    from app.webhook.sender import send_text_message

    to = payload.get("to", "")
    text = payload.get("text", "")
    if not to or not text:
        return {"error": "Missing 'to' or 'text'"}

    wa_id = await send_text_message(to, text)
    if wa_id:
        from app.store.audit import log_action
        await log_action("send_message", to, f"len={len(text)}")
        return {"status": "sent", "message_id": wa_id}
    return {"status": "failed"}
