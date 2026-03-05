"""FastAPI application entry point with APScheduler for daily analysis."""

import logging
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.store.database import init_db

# ── Pipeline status tracking ─────────────────────────────────────────
_app_start_time: float = 0.0
_last_pipeline_at: str = ""
_last_pipeline_ok: bool = True

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Scheduler ────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()


async def scheduled_daily_analysis():
    """Cron job: run the full pipeline and generate a report."""
    global _last_pipeline_at, _last_pipeline_ok
    logger.info("Scheduled daily analysis triggered")
    try:
        from app.analyzer.daily_pipeline import run_daily_pipeline
        from app.writers.report_writer import generate_daily_report

        summary = await run_daily_pipeline()
        from app.store.conversations import get_unmatched_conversations, get_overview_stats
        unmatched = await get_unmatched_conversations()
        overview = await get_overview_stats()
        report = generate_daily_report(summary, unmatched=unmatched, overview=overview)
        logger.info("Daily report:\n%s", report)
        _last_pipeline_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _last_pipeline_ok = not summary.get("errors")

        try:
            from app.writers.report_writer import write_report_to_feishu
            await write_report_to_feishu(report, summary)
        except Exception as e:
            logger.warning("CEO日报 Feishu write failed (non-blocking): %s", e)

        try:
            from app.writers.report_writer import write_report_to_notion
            await write_report_to_notion(report, summary)
        except Exception as e:
            logger.warning("CEO日报 Notion write failed (non-blocking): %s", e)
    except Exception:
        _last_pipeline_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _last_pipeline_ok = False
        logger.exception("Daily analysis failed")


# ── App lifecycle ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _app_start_time
    _app_start_time = time.time()
    await init_db()

    # Hourly interval pipeline
    if settings.pipeline_interval_hours > 0:
        scheduler.add_job(
            scheduled_daily_analysis, "interval",
            hours=settings.pipeline_interval_hours, id="interval_analysis",
        )
        logger.info("Interval pipeline enabled: every %d hour(s)", settings.pipeline_interval_hours)

    # Nightly cron
    scheduler.add_job(
        scheduled_daily_analysis, "cron",
        hour=settings.daily_analysis_hour,
        minute=settings.daily_analysis_minute,
        id="daily_analysis",
    )

    # Outbound message sync
    if settings.obsidian_sync_enabled and settings.wati_api_token:
        from app.webhook.outbound_sync import sync_outbound_messages
        scheduler.add_job(sync_outbound_messages, "interval", minutes=5, id="outbound_sync")
        logger.info("Outbound sync enabled: polling WATI every 5 minutes")

    # Feishu → HubSpot sync
    if settings.hubspot_enabled and settings.feishu_app_token:
        from app.sync.feishu_to_hubspot import sync_feishu_to_hubspot
        scheduler.add_job(sync_feishu_to_hubspot, "interval", hours=4, id="feishu_hs_sync")
        logger.info("Feishu→HubSpot sync enabled: every 4 hours")

    # Dormant customer outreach
    if settings.feishu_webhook_url and settings.dormant_outreach_interval_days > 0:
        async def _run_dormant_outreach():
            try:
                from scripts.dormant_customers import run as dormant_run
                await dormant_run(days=settings.dormant_outreach_days, dry_run=False)
            except Exception:
                logger.exception("Dormant outreach failed")

        scheduler.add_job(
            _run_dormant_outreach, "interval",
            days=settings.dormant_outreach_interval_days, id="dormant_outreach",
        )
        logger.info(
            "Dormant outreach enabled: every %d days (inactive threshold: %d days)",
            settings.dormant_outreach_interval_days, settings.dormant_outreach_days,
        )

    # Morning follow-up reminder
    if settings.feishu_webhook_url:
        from app.notifier.daily_reminder import send_daily_reminder, send_weekly_report
        scheduler.add_job(
            send_daily_reminder, "cron",
            hour=settings.reminder_hour, minute=settings.reminder_minute,
            timezone="Asia/Shanghai", id="daily_reminder",
        )
        logger.info("Daily reminder enabled: %02d:%02d CST", settings.reminder_hour, settings.reminder_minute)
        scheduler.add_job(
            send_weekly_report, "cron",
            day_of_week="sun", hour=9, minute=0,
            timezone="Asia/Shanghai", id="weekly_report",
        )
        logger.info("Weekly report enabled: Sunday 09:00 CST")

    # CEO weekly report
    if settings.feishu_app_id and settings.feishu_app_secret:
        from app.notifier.weekly_ceo_report import run_weekly_ceo_report
        scheduler.add_job(
            run_weekly_ceo_report, "cron",
            day_of_week="mon", hour=9, minute=0,
            timezone="Asia/Shanghai", id="weekly_ceo_report",
        )
        logger.info("CEO weekly report enabled: Monday 09:00 CST")

    # Feishu customer sync
    if settings.feishu_app_token and settings.feishu_table_customers:
        from app.matcher.customer_matcher import sync_from_feishu
        scheduler.add_job(sync_from_feishu, "interval", hours=4, id="feishu_customer_sync")
        logger.info("Feishu customer sync enabled: every 4 hours")

    scheduler.start()
    logger.info(
        "App started. Daily analysis scheduled at %02d:%02d",
        settings.daily_analysis_hour, settings.daily_analysis_minute,
    )

    # Startup: sync Feishu customers
    if settings.feishu_app_token and settings.feishu_table_customers:
        try:
            from app.matcher.customer_matcher import sync_from_feishu
            count = await sync_from_feishu()
            logger.info("Startup: Feishu customer sync complete (%d customers)", count)
        except Exception:
            logger.warning("Startup: Feishu customer sync failed, using existing crm_customers.json")

    # Startup: load HubSpot cache
    try:
        from app.routers.ai_manager import get_hubspot_contacts, refresh_hubspot_contacts
        t0 = time.time()
        contacts = await get_hubspot_contacts()
        if contacts:
            logger.info("HubSpot cache loaded: %d contacts in %.1fs", len(contacts), time.time() - t0)
        else:
            logger.info("No local HubSpot cache, fetching from API...")
            contacts = await refresh_hubspot_contacts()
            logger.info("HubSpot fetched: %d contacts in %.1fs", len(contacts), time.time() - t0)
    except Exception:
        logger.warning("HubSpot cache init failed (use Refresh button)")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    from app.writers.hubspot_writer import close_http_client as close_hubspot_http
    from app.writers.feishu_writer import close_http_client as close_feishu_http
    from app.writers.obsidian_forwarder import close_http_client as close_obsidian_http
    from app.webhook.sender import close_http_client as close_sender_http
    await close_hubspot_http()
    await close_feishu_http()
    await close_obsidian_http()
    await close_sender_http()
    logger.info("App stopped")


# ── FastAPI app ──────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="WhatsApp CRM Bridge",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse({"error": "rate limit exceeded"}, status_code=429)


# ── Include routers ──────────────────────────────────────────────────

from app.webhook.router import router as webhook_router        # noqa: E402
from app.feishu_bot.router import router as feishu_bot_router  # noqa: E402
from app.routers.triggers import router as triggers_router     # noqa: E402
from app.routers.dashboard import router as dashboard_router   # noqa: E402
from app.routers.ai_manager import router as ai_manager_router  # noqa: E402

app.include_router(webhook_router)
app.include_router(feishu_bot_router)
app.include_router(triggers_router)
app.include_router(dashboard_router)
app.include_router(ai_manager_router)


# ── Health & stats (core app concerns) ───────────────────────────────

@app.get("/health")
async def health():
    from app.store.database import get_db
    db_ok = False
    try:
        async with get_db() as db:
            await db.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass

    uptime_s = int(time.time() - _app_start_time) if _app_start_time else 0
    hours, remainder = divmod(uptime_s, 3600)
    minutes, seconds = divmod(remainder, 60)

    result = {
        "status": "ok" if db_ok else "degraded",
        "version": app.version,
        "uptime": f"{hours}h{minutes}m{seconds}s",
        "db": "ok" if db_ok else "failed",
        "pipeline": {
            "last_run": _last_pipeline_at or None,
            "last_ok": _last_pipeline_ok,
            "concurrency": settings.pipeline_concurrency,
            "interval_hours": settings.pipeline_interval_hours,
        },
        "services": {
            "hubspot": settings.hubspot_enabled,
            "obsidian_sync": settings.obsidian_sync_enabled,
            "auto_reply": settings.auto_reply_enabled,
            "llm_provider": settings.llm_provider,
        },
    }
    status_code = 200 if db_ok else 503
    return JSONResponse(result, status_code=status_code)


@app.get("/api/v1/stats")
async def stats():
    """Quick stats on the database."""
    from app.store.database import get_db

    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM messages")
        total_messages = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM messages WHERE processed = 0")
        unprocessed = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM conversations")
        total_conversations = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM conversations WHERE match_status = 'matched'"
        )
        matched = (await cursor.fetchone())[0]

    return {
        "total_messages": total_messages,
        "unprocessed_messages": unprocessed,
        "total_conversations": total_conversations,
        "matched_conversations": matched,
    }


@app.get("/api/v1/sync/check")
async def sync_check():
    """Check cross-system CRM sync status (Feishu ↔ HubSpot)."""
    from app.store.conversations import get_sync_status
    return await get_sync_status()
