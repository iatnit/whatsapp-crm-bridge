"""Customer analytics dashboard routes."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

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
