"""FastAPI application entry point with APScheduler for daily analysis."""

import logging
import re
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.store.database import init_db
from app.webhook.router import router as webhook_router
from app.feishu_bot.router import router as feishu_bot_router
from app.analyzer.daily_pipeline import run_daily_pipeline
from app.writers.report_writer import generate_daily_report

# ── Pipeline status tracking ─────────────────────────────────────────
_app_start_time: float = 0.0
_last_pipeline_at: str = ""
_last_pipeline_ok: bool = True

# Keep strong references to background tasks to prevent GC (Python 3.12+)
_background_tasks: set = set()

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Admin auth dependency ─────────────────────────────────────────────

async def verify_admin(
    authorization: str = Header(default=""),
    token: str = Query(default="", alias="admin_token"),
) -> None:
    """Verify admin token for management endpoints.

    Accepts: Authorization: Bearer <token>, or ?admin_token= query param.
    Skipped if ADMIN_TOKEN is not configured.
    """
    if not settings.admin_token:
        return  # no token configured = skip auth
    # Check Authorization header
    header_token = authorization.removeprefix("Bearer ").strip()
    if header_token == settings.admin_token:
        return
    # Check query param
    if token == settings.admin_token:
        return
    raise HTTPException(status_code=401, detail="unauthorized")


# ── Scheduler ────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()


async def scheduled_daily_analysis():
    """Cron job: run the full pipeline and generate a report."""
    global _last_pipeline_at, _last_pipeline_ok
    logger.info("Scheduled daily analysis triggered")
    try:
        summary = await run_daily_pipeline()
        from app.store.conversations import get_unmatched_conversations, get_overview_stats
        unmatched = await get_unmatched_conversations()
        overview = await get_overview_stats()
        report = generate_daily_report(summary, unmatched=unmatched, overview=overview)
        logger.info("Daily report:\n%s", report)
        _last_pipeline_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _last_pipeline_ok = not summary.get("errors")

        # Write report to Feishu CEO日报 Base
        try:
            from app.writers.report_writer import write_report_to_feishu
            await write_report_to_feishu(report, summary)
        except Exception as e:
            logger.warning("CEO日报 Feishu write failed (non-blocking): %s", e)

        # Write report to Notion
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
    # Startup
    global _app_start_time
    _app_start_time = time.time()
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

    # Outbound message sync: poll WATI API every 5 min to capture agent replies
    if settings.obsidian_sync_enabled and settings.wati_api_token:
        from app.webhook.outbound_sync import sync_outbound_messages
        scheduler.add_job(
            sync_outbound_messages,
            "interval",
            minutes=5,
            id="outbound_sync",
        )
        logger.info("Outbound sync enabled: polling WATI every 5 minutes")

    # Feishu 跟进记录 → HubSpot Notes sync (every 4 hours)
    # Table has 16k+ records; full scan takes ~3 min (no server-side date filter support)
    if settings.hubspot_enabled and settings.feishu_app_token:
        from app.sync.feishu_to_hubspot import sync_feishu_to_hubspot
        scheduler.add_job(
            sync_feishu_to_hubspot,
            "interval",
            hours=4,
            id="feishu_hs_sync",
        )
        logger.info("Feishu→HubSpot sync enabled: every 4 hours")

    # Bi-monthly dormant customer outreach (every 15 days by default)
    if settings.feishu_webhook_url and settings.dormant_outreach_interval_days > 0:
        async def _run_dormant_outreach():
            try:
                from scripts.dormant_customers import run as dormant_run
                await dormant_run(days=settings.dormant_outreach_days, dry_run=False)
            except Exception:
                logger.exception("Dormant outreach failed")

        scheduler.add_job(
            _run_dormant_outreach,
            "interval",
            days=settings.dormant_outreach_interval_days,
            id="dormant_outreach",
        )
        logger.info(
            "Dormant outreach enabled: every %d days (inactive threshold: %d days)",
            settings.dormant_outreach_interval_days, settings.dormant_outreach_days,
        )

    # Morning follow-up reminder (CST timezone)
    if settings.feishu_webhook_url:
        from app.notifier.daily_reminder import send_daily_reminder, send_weekly_report
        scheduler.add_job(
            send_daily_reminder,
            "cron",
            hour=settings.reminder_hour,
            minute=settings.reminder_minute,
            timezone="Asia/Shanghai",
            id="daily_reminder",
        )
        logger.info(
            "Daily reminder enabled: %02d:%02d CST → Feishu webhook",
            settings.reminder_hour, settings.reminder_minute,
        )
        # Weekly report every Sunday 9am CST
        scheduler.add_job(
            send_weekly_report,
            "cron",
            day_of_week="sun",
            hour=9,
            minute=0,
            timezone="Asia/Shanghai",
            id="weekly_report",
        )
        logger.info("Weekly report enabled: Sunday 09:00 CST → Feishu webhook")

    scheduler.start()
    logger.info(
        "App started. Daily analysis scheduled at %02d:%02d",
        settings.daily_analysis_hour,
        settings.daily_analysis_minute,
    )

    # Load HubSpot cache from disk (instant); fetch from API only if no local file
    try:
        t0 = time.time()
        contacts = await _get_hubspot_contacts()
        if contacts:
            logger.info("HubSpot cache loaded: %d contacts in %.1fs", len(contacts), time.time() - t0)
        else:
            logger.info("No local HubSpot cache, fetching from API...")
            contacts = await _refresh_hubspot_contacts()
            logger.info("HubSpot fetched: %d contacts in %.1fs", len(contacts), time.time() - t0)
    except Exception:
        logger.warning("HubSpot cache init failed (use Refresh button)")
    yield
    # Shutdown
    scheduler.shutdown(wait=False)
    # Close shared httpx clients
    from app.writers.hubspot_writer import close_http_client as close_hubspot_http
    from app.writers.feishu_writer import close_http_client as close_feishu_http
    from app.writers.obsidian_forwarder import close_http_client as close_obsidian_http
    await close_hubspot_http()
    await close_feishu_http()
    await close_obsidian_http()
    logger.info("App stopped")


# ── FastAPI app ──────────────────────────────────────────────────────

app = FastAPI(
    title="WhatsApp CRM Bridge",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(webhook_router)
app.include_router(feishu_bot_router)


# ── Health & manual trigger endpoints ────────────────────────────────

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


@app.post("/api/v1/analyze/trigger", dependencies=[Depends(verify_admin)])
async def manual_trigger():
    """Manually trigger the daily analysis pipeline (for testing)."""
    summary = await run_daily_pipeline()
    from app.store.conversations import get_unmatched_conversations, get_overview_stats
    unmatched = await get_unmatched_conversations()
    overview = await get_overview_stats()
    report = generate_daily_report(summary, unmatched=unmatched, overview=overview)
    try:
        from app.writers.report_writer import write_report_to_feishu
        await write_report_to_feishu(report, summary)
    except Exception as e:
        logger.warning("Manual trigger Feishu write failed: %s", e)
    try:
        from app.writers.report_writer import write_report_to_notion
        await write_report_to_notion(report, summary)
    except Exception as e:
        logger.warning("Manual trigger Notion write failed: %s", e)
    return {"summary": summary, "report": report}


@app.post("/api/v1/feishu-hs-sync/trigger", dependencies=[Depends(verify_admin)])
async def manual_feishu_hs_sync():
    """Manually trigger Feishu 跟进记录 → HubSpot Notes sync (runs in background)."""
    import asyncio
    from app.sync.feishu_to_hubspot import sync_feishu_to_hubspot
    task = asyncio.create_task(sync_feishu_to_hubspot())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "started", "message": "Sync running in background, check logs for progress"}


@app.post("/api/v1/reminder/trigger", dependencies=[Depends(verify_admin)])
async def manual_reminder():
    """Manually trigger the daily follow-up reminder (for testing)."""
    from app.notifier.daily_reminder import send_daily_reminder
    sent = await send_daily_reminder()
    return {"sent": sent}


@app.post("/api/v1/dormant/trigger", dependencies=[Depends(verify_admin)])
async def manual_dormant_outreach(days: int = 30):
    """Manually trigger dormant customer outreach report to Feishu."""
    import asyncio
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scripts.dormant_customers import run as dormant_run
    task = asyncio.create_task(dormant_run(days=days, dry_run=False))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "started", "days": days}


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


# ── Customer Dashboard ───────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CRM 客户看板 — LOCACRYSTAL</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;color:#1e293b;padding:24px}
h1{font-size:1.5rem;margin-bottom:20px;color:#0f172a}
h3{font-size:1rem;color:#475569;margin-bottom:12px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.kpi{background:#fff;border-radius:12px;padding:20px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.kpi .n{font-size:2.2rem;font-weight:700;color:#2563eb}
.kpi .l{color:#64748b;font-size:.85rem;margin-top:6px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
table{width:100%;border-collapse:collapse;font-size:.875rem}
th{text-align:left;padding:8px 10px;border-bottom:2px solid #e2e8f0;color:#64748b;font-weight:600}
td{padding:8px 10px;border-bottom:1px solid #f1f5f9}
tr:hover td{background:#f8fafc}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.75rem;font-weight:700}
.S{background:#fef2f2;color:#dc2626}.A{background:#fffbeb;color:#d97706}
.B{background:#eff6ff;color:#2563eb}.C{background:#f1f5f9;color:#475569}
.D{background:#f8fafc;color:#94a3b8}
.ts{color:#94a3b8;font-size:.75rem;text-align:right;margin-top:16px}
@media(max-width:768px){.kpis,.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>📊 LOCACRYSTAL CRM 客户看板</h1>
<div class="kpis" id="kpis"></div>
<div class="grid">
  <div class="card"><h3>Tier 分布</h3><canvas id="tierChart"></canvas></div>
  <div class="card"><h3>跟进优先级</h3><canvas id="prioChart"></canvas></div>
  <div class="card" style="grid-column:span 2"><canvas id="msgChart"></canvas></div>
  <div class="card" style="grid-column:span 2">
    <h3>消息最多客户 Top 10</h3>
    <table><thead><tr><th>客户</th><th>Tier</th><th>消息数</th><th>最后联系</th></tr></thead>
    <tbody id="topTb"></tbody></table>
  </div>
</div>
<p class="ts" id="ts"></p>
<script>
const TIER_COLORS={'S':'#dc2626','A':'#d97706','B':'#2563eb','C':'#94a3b8','D':'#cbd5e1','未设置':'#e2e8f0'};
const PRIO_COLORS={'high':'#ef4444','medium':'#f59e0b','normal':'#22c55e','low':'#22c55e'};
const PRIO_LABEL={'high':'高优先','medium':'中优先','normal':'普通','low':'低优先'};
async function load(){
  const token=new URLSearchParams(location.search).get('admin_token')||'';
  const url='/api/v1/dashboard/data'+(token?'?admin_token='+encodeURIComponent(token):'');
  const d=await fetch(url).then(r=>r.json());
  document.getElementById('kpis').innerHTML=[
    {n:d.total_customers,l:'总客户'},{n:d.active_7d,l:'7天活跃'},
    {n:d.hot_leads,l:'今日热线索'},{n:d.new_7d,l:'本周新增'}
  ].map(k=>`<div class="kpi"><div class="n">${k.n}</div><div class="l">${k.l}</div></div>`).join('');
  new Chart(document.getElementById('tierChart'),{type:'doughnut',data:{
    labels:d.tiers.map(t=>t.tier),
    datasets:[{data:d.tiers.map(t=>t.count),backgroundColor:d.tiers.map(t=>TIER_COLORS[t.tier]||'#e2e8f0'),borderWidth:2}]
  },options:{plugins:{legend:{position:'right'}}}});
  new Chart(document.getElementById('prioChart'),{type:'doughnut',data:{
    labels:d.priorities.map(p=>PRIO_LABEL[p.priority]||p.priority),
    datasets:[{data:d.priorities.map(p=>p.count),backgroundColor:d.priorities.map(p=>PRIO_COLORS[p.priority]||'#94a3b8'),borderWidth:2}]
  },options:{plugins:{legend:{position:'right'}}}});
  new Chart(document.getElementById('msgChart'),{type:'line',data:{
    labels:d.msg_7d.map(m=>m.date),
    datasets:[
      {label:'收到消息',data:d.msg_7d.map(m=>m.inbound),borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.1)',fill:true,tension:.3},
      {label:'发送消息',data:d.msg_7d.map(m=>m.outbound),borderColor:'#10b981',backgroundColor:'rgba(16,185,129,.1)',fill:true,tension:.3}
    ]
  },options:{plugins:{title:{display:true,text:'7天消息量趋势'}},scales:{y:{beginAtZero:true}}}});
  document.getElementById('topTb').innerHTML=d.top_customers.map(c=>{
    const badge=c.tier?`<span class="badge ${c.tier}">${c.tier}</span>`:'<span style="color:#cbd5e1">-</span>';
    return `<tr><td>${c.name}</td><td>${badge}</td><td>${c.msgs}</td><td>${c.last_contact||'-'}</td></tr>`;
  }).join('');
  document.getElementById('ts').textContent='更新时间: '+new Date().toLocaleString('zh-CN');
}
load();
</script></body></html>"""


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
async def dashboard():
    """Customer analytics dashboard."""
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/api/v1/dashboard/data", dependencies=[Depends(verify_admin)])
async def dashboard_data():
    """Return aggregated CRM stats for the dashboard."""
    from app.store.conversations import get_overview_stats
    return await get_overview_stats()


# ── AI Manager UI & API ─────────────────────────────────────────────

_ai_manager_html: str | None = None

# HubSpot contact cache — persisted to data/hubspot_contacts.json
_hubspot_cache: list[dict] | None = None
_HUBSPOT_CACHE_FILE = Path(__file__).parent.parent / "data" / "hubspot_contacts.json"

_VALID_TAGS = {"hot_lead", "vip", "repeat_buyer", "first_timer", "price_shopper", "risky", "agent_potential"}


def _digits(phone: str) -> str:
    """Strip all non-digit chars for phone matching."""
    return re.sub(r"\D", "", phone or "")


def _load_hubspot_from_disk() -> list[dict] | None:
    """Load cached HubSpot contacts from local JSON file."""
    try:
        if _HUBSPOT_CACHE_FILE.exists():
            import json
            data = json.loads(_HUBSPOT_CACHE_FILE.read_text())
            logger.info("Loaded %d HubSpot contacts from disk cache", len(data))
            return data
    except Exception:
        logger.warning("Failed to read HubSpot disk cache, will fetch from API")
    return None


def _save_hubspot_to_disk(contacts: list[dict]) -> None:
    """Persist HubSpot contacts to local JSON file."""
    try:
        import json
        _HUBSPOT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HUBSPOT_CACHE_FILE.write_text(json.dumps(contacts, ensure_ascii=False))
        logger.info("Saved %d HubSpot contacts to disk cache", len(contacts))
    except Exception:
        logger.warning("Failed to write HubSpot disk cache")


async def _get_hubspot_contacts() -> list[dict]:
    """Return in-memory HubSpot contacts. Load from disk on first call."""
    global _hubspot_cache
    if _hubspot_cache is not None:
        return _hubspot_cache
    _hubspot_cache = _load_hubspot_from_disk() or []
    return _hubspot_cache


async def _refresh_hubspot_contacts() -> list[dict]:
    """Pull fresh contacts from HubSpot API, update memory + disk."""
    global _hubspot_cache
    from app.writers.hubspot_writer import list_all_contacts
    _hubspot_cache = await list_all_contacts()
    _save_hubspot_to_disk(_hubspot_cache)
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
    from app.store.conversations import get_all_conversations

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
        # Inline relationship_stage calc (avoids N+1 DB queries)
        total = c.get("total_messages") or 0
        from app.store.conversations import _parse_first_message_ts, calc_relationship_stage
        first_ts = _parse_first_message_ts(c.get("first_message_at"))
        first_seen_days = max(0, int((time.time() - first_ts) / 86400)) if first_ts else 0
        rel_stage = calc_relationship_stage(total, first_seen_days)

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
            "customer_size": c.get("customer_size") or "",
            "relationship_stage": rel_stage,
            "intent_priority": c.get("intent_priority") or "",
            "intent_tags": c.get("intent_tags") or "",
            "source": "both" if hs else "local",
            "hubspot_id": hs["id"] if hs else None,
            "customer_stage": (hs or {}).get("customer_stage") or "",
            "product_interest": (hs or {}).get("product_interest") or "",
            "customer_tags": (hs or {}).get("customer_tags") or "",
            "customer_type": (hs or {}).get("customer_type") or "",
            "industry": (hs or {}).get("industry") or "",
            "customer_tier": (hs or {}).get("customer_tier") or "",
        }
        customers.append(entry)

    # 4) HubSpot-only contacts (not in local)
    for h in hs_contacts:
        phone_key = _digits(h.get("phone") or h.get("whatsapp_number") or "")
        if not phone_key or phone_key in seen_hs_keys:
            continue
        seen_hs_keys.add(phone_key)
        name_parts = [h.get("firstname") or "", h.get("lastname") or ""]
        display = " ".join(p for p in name_parts if p).strip()
        customers.append({
            "phone": h.get("phone") or h.get("whatsapp_number") or "",
            "display_name": display,
            "customer_name": display,
            "match_status": "hubspot_only",
            "total_messages": 0,
            "ai_disabled": 0,
            "customer_size": "",
            "relationship_stage": "",
            "source": "hubspot",
            "hubspot_id": h["id"],
            "customer_stage": h.get("customer_stage") or "",
            "product_interest": h.get("product_interest") or "",
            "customer_tags": h.get("customer_tags") or "",
            "customer_type": h.get("customer_type") or "",
            "industry": h.get("industry") or "",
            "customer_tier": h.get("customer_tier") or "",
        })

    return {"count": len(customers), "customers": customers}


@app.post("/api/v1/ai/disable/{phone}", dependencies=[Depends(verify_admin)])
async def disable_ai(phone: str):
    """Disable AI auto-reply for a customer (big/VIP, handled manually)."""
    from app.store.conversations import set_ai_disabled
    found = await set_ai_disabled(phone, disabled=True)
    if not found:
        return {"error": f"Phone {phone} not found in conversations"}
    return {"status": "ok", "phone": phone, "ai_disabled": True}


@app.post("/api/v1/ai/enable/{phone}", dependencies=[Depends(verify_admin)])
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


_VALID_SIZES = {"big", "medium", "small", ""}


@app.post("/api/v1/ai/customer-size/{phone}", dependencies=[Depends(verify_admin)])
async def set_customer_size_api(phone: str, payload: dict):
    """Set customer size classification.

    Body: {"size": "big"}  — valid: big, medium, small, "" (none)
    """
    from app.store.conversations import set_customer_size
    size = payload.get("size", "")
    if size not in _VALID_SIZES:
        return JSONResponse({"error": f"Invalid size: {size}"}, status_code=400)
    found = await set_customer_size(phone, size)
    if not found:
        return JSONResponse({"error": f"Phone {phone} not found"}, status_code=404)
    return {"status": "ok", "phone": phone, "customer_size": size}


@app.post("/api/v1/ai/tags/{phone}", dependencies=[Depends(verify_admin)])
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

    # Update local cache in-place so no full re-fetch needed
    if _hubspot_cache:
        phone_digits = _digits(phone)
        for h in _hubspot_cache:
            for field in ("phone", "whatsapp_number"):
                if _digits(h.get(field) or "") == phone_digits:
                    h["customer_tags"] = tags_str
                    break
        _save_hubspot_to_disk(_hubspot_cache)
    return {"status": "ok", "phone": phone, "tags": tags_str}


@app.post("/api/v1/ai/refresh", dependencies=[Depends(verify_admin)])
async def refresh_cache():
    """Pull fresh HubSpot contacts and update local cache."""
    t0 = time.time()
    contacts = await _refresh_hubspot_contacts()
    elapsed = round(time.time() - t0, 1)
    return {"status": "ok", "count": len(contacts), "seconds": elapsed}


@app.post("/api/v1/send", dependencies=[Depends(verify_admin)])
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
