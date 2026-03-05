"""Customer analytics dashboard routes."""

import csv
import io
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from app.auth import verify_admin

router = APIRouter(tags=["dashboard"])

_STATIC_DIR = Path(__file__).parent.parent / "static"


@router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
async def dashboard():
    """Customer analytics dashboard."""
    return HTMLResponse(_load_html("dashboard.html"))


@router.get("/api/v1/dashboard/data", dependencies=[Depends(verify_admin)])
async def dashboard_data():
    """Return aggregated CRM stats for the dashboard."""
    from app.store.conversations import get_overview_stats
    return await get_overview_stats()


@router.get("/customer/{phone}", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
async def customer_detail_page(phone: str):
    """Customer detail page with message history."""
    return HTMLResponse(_load_html("customer-detail.html"))


@router.get("/api/v1/customer/{phone}", dependencies=[Depends(verify_admin)])
async def customer_detail_api(phone: str):
    """Return customer profile + recent messages."""
    from app.store.database import get_db
    from app.store.messages import get_messages_by_phone

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM conversations WHERE phone = ?", (phone,)
        )
        row = await cursor.fetchone()
        profile = dict(row) if row else {}

    messages = await get_messages_by_phone(phone, limit=500)
    # Reverse to chronological order
    messages.reverse()

    return {"profile": profile, "messages": messages}


@router.get("/api/v1/dashboard/search", dependencies=[Depends(verify_admin)])
async def search_messages(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=50, le=200),
):
    """Full-text search across messages and customer names."""
    from app.store.database import get_db

    results = []
    async with get_db() as db:
        # Search messages content
        cursor = await db.execute(
            """SELECT m.phone, m.display_name, m.direction, m.content,
                      m.msg_type, m.timestamp,
                      c.customer_name, c.customer_tier
               FROM messages m
               LEFT JOIN conversations c ON m.phone = c.phone
               WHERE m.content LIKE ?
               ORDER BY m.timestamp DESC LIMIT ?""",
            (f"%{q}%", limit),
        )
        for row in await cursor.fetchall():
            r = dict(row)
            r["match_type"] = "message"
            results.append(r)

        # Search customer names
        cursor = await db.execute(
            """SELECT phone, display_name, customer_name, customer_tier,
                      total_messages, last_message_at, location
               FROM conversations
               WHERE display_name LIKE ? OR customer_name LIKE ?
               ORDER BY total_messages DESC LIMIT ?""",
            (f"%{q}%", f"%{q}%", limit),
        )
        for row in await cursor.fetchall():
            r = dict(row)
            r["match_type"] = "customer"
            results.append(r)

    return {"query": q, "results": results}


# ── CSV Export ────────────────────────────────────────────────────────

_CSV_BOM = "\ufeff"  # BOM for Excel UTF-8 compatibility


@router.get("/api/v1/export/customers", dependencies=[Depends(verify_admin)])
async def export_customers():
    """Export all customers as CSV."""
    from app.store.database import get_db

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM conversations ORDER BY last_message_at DESC"
        )
        rows = await cursor.fetchall()

    output = io.StringIO()
    output.write(_CSV_BOM)
    writer = csv.writer(output)
    writer.writerow([
        "phone", "display_name", "customer_name", "match_status",
        "customer_tier", "customer_size", "location", "total_messages",
        "first_message_at", "last_message_at", "intent_priority",
        "intent_tags", "ai_disabled", "customer_stage", "product_interest",
    ])
    for r in rows:
        row = dict(r)
        writer.writerow([
            row.get("phone", ""), row.get("display_name", ""),
            row.get("customer_name", ""), row.get("match_status", ""),
            row.get("customer_tier", ""), row.get("customer_size", ""),
            row.get("location", ""), row.get("total_messages", 0),
            row.get("first_message_at", ""), row.get("last_message_at", ""),
            row.get("intent_priority", ""), row.get("intent_tags", ""),
            row.get("ai_disabled", 0), row.get("customer_stage", ""),
            row.get("product_interest", ""),
        ])

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


@router.get("/api/v1/export/messages", dependencies=[Depends(verify_admin)])
async def export_messages(phone: str = Query(default="")):
    """Export messages as CSV. Optional phone filter."""
    from app.store.database import get_db

    async with get_db() as db:
        if phone:
            cursor = await db.execute(
                "SELECT * FROM messages WHERE phone = ? ORDER BY timestamp",
                (phone,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM messages ORDER BY timestamp DESC LIMIT 10000"
            )
        rows = await cursor.fetchall()

    output = io.StringIO()
    output.write(_CSV_BOM)
    writer = csv.writer(output)
    writer.writerow(["phone", "display_name", "direction", "msg_type",
                     "content", "timestamp", "media_path"])
    for r in rows:
        row = dict(r)
        writer.writerow([
            row.get("phone", ""), row.get("display_name", ""),
            row.get("direction", ""), row.get("msg_type", ""),
            row.get("content", ""), row.get("timestamp", ""),
            row.get("media_path", ""),
        ])

    filename = f"messages_{phone}.csv" if phone else "messages_all.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Audit Log ─────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
async def audit_page():
    """Audit log page."""
    return HTMLResponse(_load_html("audit.html"))


@router.get("/api/v1/audit/logs", dependencies=[Depends(verify_admin)])
async def audit_logs(limit: int = Query(default=200, le=1000)):
    """Return recent audit log entries."""
    from app.store.audit import get_recent_logs
    logs = await get_recent_logs(limit)
    return {"count": len(logs), "logs": logs}


# ── HTML file loading ────────────────────────────────────────────────

_html_cache: dict[str, tuple[str, float]] = {}  # filename → (content, mtime)


def _load_html(filename: str) -> str:
    """Load HTML with mtime-based cache (auto-refresh on file change)."""
    path = _STATIC_DIR / filename
    if not path.exists():
        return f"<html><body><h1>{filename} — file missing</h1></body></html>"
    mtime = path.stat().st_mtime
    cached = _html_cache.get(filename)
    if cached and cached[1] == mtime:
        return cached[0]
    content = path.read_text()
    _html_cache[filename] = (content, mtime)
    return content
