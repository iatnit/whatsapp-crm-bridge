"""FastAPI application entry point with APScheduler for daily analysis."""

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.config import settings
from app.store.database import init_db
from app.webhook.router import router as webhook_router
from app.analyzer.daily_pipeline import run_daily_pipeline
from app.writers.report_writer import generate_daily_report

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
    logger.info("Scheduled daily analysis triggered")
    try:
        summary = await run_daily_pipeline()
        report = generate_daily_report(summary)
        logger.info("Daily report:\n%s", report)
    except Exception:
        logger.exception("Daily analysis failed")


# ── App lifecycle ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    scheduler.add_job(
        scheduled_daily_analysis,
        "cron",
        hour=settings.daily_analysis_hour,
        minute=settings.daily_analysis_minute,
        id="daily_analysis",
    )
    scheduler.start()
    logger.info(
        "App started. Daily analysis scheduled at %02d:%02d",
        settings.daily_analysis_hour,
        settings.daily_analysis_minute,
    )
    yield
    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("App stopped")


# ── FastAPI app ──────────────────────────────────────────────────────

app = FastAPI(
    title="WhatsApp CRM Bridge",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(webhook_router)


# ── Health & manual trigger endpoints ────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/v1/analyze/trigger")
async def manual_trigger():
    """Manually trigger the daily analysis pipeline (for testing)."""
    summary = await run_daily_pipeline()
    report = generate_daily_report(summary)
    return {"summary": summary, "report": report}


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
