"""FastAPI application entry point with APScheduler for daily analysis."""

import logging
import re
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

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
        from app.store.conversations import get_unmatched_conversations
        unmatched = await get_unmatched_conversations()
        report = generate_daily_report(summary, unmatched=unmatched)
        logger.info("Daily report:\n%s", report)
    except Exception:
        logger.exception("Daily analysis failed")


# ── App lifecycle ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()

    # Hourly interval pipeline (if enabled)
    if settings.pipeline_interval_hours > 0:
        scheduler.add_job(
            scheduled_daily_analysis,
            "interval",
            hours=settings.pipeline_interval_hours,
            id="interval_analysis",
        )
        logger.info(
            "Interval pipeline enabled: every %d hour(s)",
            settings.pipeline_interval_hours,
        )

    # Keep nightly summary as well
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
    # Close shared httpx clients
    from app.writers.hubspot_writer import close_http_client as close_hubspot_http
    from app.writers.feishu_writer import close_http_client as close_feishu_http
    await close_hubspot_http()
    await close_feishu_http()
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
    from app.store.database import get_db
    try:
        async with get_db() as db:
            await db.execute("SELECT 1")
        return {"status": "ok", "db": "ok"}
    except Exception:
        return JSONResponse({"status": "error", "db": "failed"}, status_code=503)


@app.post("/api/v1/analyze/trigger")
async def manual_trigger():
    """Manually trigger the daily analysis pipeline (for testing)."""
    summary = await run_daily_pipeline()
    from app.store.conversations import get_unmatched_conversations
    unmatched = await get_unmatched_conversations()
    report = generate_daily_report(summary, unmatched=unmatched)
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


@app.get("/api/v1/sync/check")
async def sync_check():
    """Check cross-system CRM sync status (Feishu ↔ HubSpot).

    Reports how many conversations have Feishu record_id, HubSpot contact_id,
    both, or neither. Also lists conversations with missing links.
    """
    from app.store.conversations import get_sync_status
    return await get_sync_status()


# ── AI Manager UI & API ─────────────────────────────────────────────

_ai_manager_html: str | None = None

# HubSpot contact cache (TTL 300s)
_hubspot_cache: list[dict] | None = None
_hubspot_cache_ts: float = 0
_HUBSPOT_CACHE_TTL = 300

_VALID_TAGS = {"hot_lead", "vip", "repeat_buyer", "first_timer", "price_shopper", "risky", "agent_potential"}


def _digits(phone: str) -> str:
    """Strip all non-digit chars for phone matching."""
    return re.sub(r"\D", "", phone or "")


async def _get_hubspot_contacts() -> list[dict]:
    """Return cached HubSpot contacts, refreshing if stale."""
    global _hubspot_cache, _hubspot_cache_ts
    if _hubspot_cache is not None and (time.time() - _hubspot_cache_ts) < _HUBSPOT_CACHE_TTL:
        return _hubspot_cache
    from app.writers.hubspot_writer import list_all_contacts
    _hubspot_cache = await list_all_contacts()
    _hubspot_cache_ts = time.time()
    return _hubspot_cache


@app.get("/ai-manager", response_class=HTMLResponse)
async def ai_manager_page():
    """Serve the AI Manager single-page UI."""
    global _ai_manager_html
    if _ai_manager_html is None:
        _ai_manager_html = (
            Path(__file__).parent / "static" / "ai-manager.html"
        ).read_text()
    return _ai_manager_html


@app.get("/api/v1/ai/customers")
async def list_ai_customers():
    """Return merged local + HubSpot customers for the manager UI."""
    from app.store.conversations import get_all_conversations, get_customer_context

    # 1) Local conversations
    convs = await get_all_conversations()

    # 2) HubSpot contacts
    hs_contacts = await _get_hubspot_contacts()

    # Index HubSpot by digits-only phone
    hs_by_phone: dict[str, dict] = {}
    for h in hs_contacts:
        for field in ("phone", "whatsapp_number"):
            key = _digits(h.get(field, ""))
            if key and len(key) >= 7:
                hs_by_phone[key] = h

    seen_hs_keys: set[str] = set()
    customers: list[dict] = []

    # 3) Build merged list: local conversations enriched with HubSpot data
    for c in convs:
        ctx = await get_customer_context(c["phone"])
        phone_key = _digits(c["phone"])
        hs = hs_by_phone.get(phone_key)
        if hs:
            seen_hs_keys.add(phone_key)

        entry = {
            "phone": c["phone"],
            "display_name": c.get("display_name", ""),
            "customer_name": c.get("customer_name", ""),
            "match_status": c.get("match_status", "unmatched"),
            "total_messages": c.get("total_messages", 0),
            "ai_disabled": c.get("ai_disabled", 0),
            "relationship_stage": ctx["relationship_stage"],
            "source": "both" if hs else "local",
            "hubspot_id": hs["id"] if hs else None,
            "customer_stage": (hs or {}).get("customer_stage", ""),
            "product_interest": (hs or {}).get("product_interest", ""),
            "customer_tags": (hs or {}).get("customer_tags", ""),
            "customer_type": (hs or {}).get("customer_type", ""),
            "industry": (hs or {}).get("industry", ""),
            "customer_tier": (hs or {}).get("customer_tier", ""),
        }
        customers.append(entry)

    # 4) HubSpot-only contacts (not in local)
    for h in hs_contacts:
        phone_key = _digits(h.get("phone", "") or h.get("whatsapp_number", ""))
        if not phone_key or phone_key in seen_hs_keys:
            continue
        seen_hs_keys.add(phone_key)
        name_parts = [h.get("firstname", ""), h.get("lastname", "")]
        display = " ".join(p for p in name_parts if p).strip()
        customers.append({
            "phone": h.get("phone", "") or h.get("whatsapp_number", "") or "",
            "display_name": display,
            "customer_name": display,
            "match_status": "hubspot_only",
            "total_messages": 0,
            "ai_disabled": 0,
            "relationship_stage": "",
            "source": "hubspot",
            "hubspot_id": h["id"],
            "customer_stage": h.get("customer_stage", ""),
            "product_interest": h.get("product_interest", ""),
            "customer_tags": h.get("customer_tags", ""),
            "customer_type": h.get("customer_type", ""),
            "industry": h.get("industry", ""),
            "customer_tier": h.get("customer_tier", ""),
        })

    return {"count": len(customers), "customers": customers}


@app.post("/api/v1/ai/disable/{phone}")
async def disable_ai(phone: str):
    """Disable AI auto-reply for a customer (big/VIP, handled manually)."""
    from app.store.conversations import set_ai_disabled
    found = await set_ai_disabled(phone, disabled=True)
    if not found:
        return {"error": f"Phone {phone} not found in conversations"}
    return {"status": "ok", "phone": phone, "ai_disabled": True}


@app.post("/api/v1/ai/enable/{phone}")
async def enable_ai(phone: str):
    """Re-enable AI auto-reply for a customer."""
    from app.store.conversations import set_ai_disabled
    found = await set_ai_disabled(phone, disabled=False)
    if not found:
        return {"error": f"Phone {phone} not found in conversations"}
    return {"status": "ok", "phone": phone, "ai_disabled": False}


@app.get("/api/v1/ai/disabled")
async def list_ai_disabled():
    """List all customers with AI auto-reply disabled."""
    from app.store.conversations import get_ai_disabled_list
    customers = await get_ai_disabled_list()
    return {"count": len(customers), "customers": customers}


@app.post("/api/v1/ai/tags/{phone}")
async def update_tags(phone: str, payload: dict):
    """Update customer_tags on the HubSpot contact matching this phone.

    Body: {"tags": "hot_lead;vip"}
    """
    global _hubspot_cache
    from app.writers.hubspot_writer import search_contact_by_phone, update_customer_tags

    tags_str = payload.get("tags", "")
    # Validate each tag
    if tags_str:
        for tag in tags_str.split(";"):
            tag = tag.strip()
            if tag and tag not in _VALID_TAGS:
                return JSONResponse({"error": f"Invalid tag: {tag}"}, status_code=400)

    contact_id = await search_contact_by_phone(phone)
    if not contact_id:
        return JSONResponse({"error": f"HubSpot contact not found for {phone}"}, status_code=404)

    ok = await update_customer_tags(contact_id, tags_str)
    if not ok:
        return JSONResponse({"error": "HubSpot update failed"}, status_code=502)

    # Invalidate cache so next list reflects the change
    _hubspot_cache = None
    return {"status": "ok", "phone": phone, "tags": tags_str}


@app.post("/api/v1/ai/refresh")
async def refresh_cache():
    """Force-clear the HubSpot contact cache."""
    global _hubspot_cache
    _hubspot_cache = None
    return {"status": "ok", "message": "Cache cleared"}


@app.post("/api/v1/send")
async def send_message(payload: dict):
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
        return {"status": "sent", "message_id": wa_id}
    return {"status": "failed"}
