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


# ── Editable fields: local fields + HubSpot-synced fields ────────────

# Fields stored in local conversations table
_LOCAL_FIELDS = {
    "customer_name", "display_name", "location", "customer_size",
    "customer_tier", "product_interest", "customer_stage", "intent_priority",
}

# Map from local field name to HubSpot property name
_HUBSPOT_FIELD_MAP = {
    "customer_name": "firstname",   # will split to firstname/lastname
    "location": "country",
    "customer_tier": "customer_tier",
    "product_interest": "product_interest",
    "customer_stage": "customer_stage",
}


@router.post("/api/v1/customer/{phone}/update", dependencies=[Depends(verify_admin)])
async def update_customer_profile(phone: str, payload: dict):
    """Update customer profile fields and sync to HubSpot.

    Body: {"customer_name": "John", "location": "India", ...}
    Only provided keys are updated; missing keys are left unchanged.
    """
    from app.store.database import get_db
    from app.store.audit import log_action

    # Filter to valid fields only
    updates = {k: v for k, v in payload.items() if k in _LOCAL_FIELDS}
    if not updates:
        return {"error": "No valid fields to update"}, 400

    # Update local SQLite
    async with get_db() as db:
        for field, value in updates.items():
            await db.execute(
                f"UPDATE conversations SET {field} = ? WHERE phone = ?",
                (value, phone),
            )
        await db.commit()

    # Sync to HubSpot if contact exists
    hs_synced = False
    try:
        from app.writers.hubspot_writer import search_contact_by_phone, update_contact
        contact_id = await search_contact_by_phone(phone)
        if contact_id:
            hs_props = {}
            for field, value in updates.items():
                hs_key = _HUBSPOT_FIELD_MAP.get(field)
                if hs_key and value:
                    if field == "customer_name":
                        parts = value.strip().split(maxsplit=1)
                        hs_props["firstname"] = parts[0]
                        if len(parts) > 1:
                            hs_props["lastname"] = parts[1]
                    else:
                        hs_props[hs_key] = value
            if hs_props:
                hs_synced = await update_contact(contact_id, extra=hs_props)
    except Exception:
        pass  # HubSpot sync is best-effort

    changed = ", ".join(f"{k}={v}" for k, v in updates.items())
    await log_action("update_profile", phone, changed)

    return {"status": "ok", "phone": phone, "updated": list(updates.keys()), "hubspot_synced": hs_synced}


# ── Customer Notes ─────────────────────────────────────────────────

@router.get("/api/v1/customer/{phone}/notes", dependencies=[Depends(verify_admin)])
async def get_customer_notes(phone: str):
    """Return all notes for a customer."""
    from app.store.database import get_db
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, content, created_at FROM customer_notes "
            "WHERE phone = ? ORDER BY created_at DESC",
            (phone,),
        )
        notes = [dict(row) for row in await cursor.fetchall()]
    return {"phone": phone, "notes": notes}


@router.post("/api/v1/customer/{phone}/notes", dependencies=[Depends(verify_admin)])
async def add_customer_note(phone: str, payload: dict):
    """Add a note to a customer. Body: {"content": "..."}"""
    from app.store.database import get_db
    from app.store.audit import log_action

    content = (payload.get("content") or "").strip()
    if not content:
        return {"error": "Empty note"}, 400

    async with get_db() as db:
        await db.execute(
            "INSERT INTO customer_notes (phone, content) VALUES (?, ?)",
            (phone, content),
        )
        await db.commit()

    await log_action("add_note", phone, content[:80])
    return {"status": "ok"}


@router.delete("/api/v1/customer/{phone}/notes/{note_id}", dependencies=[Depends(verify_admin)])
async def delete_customer_note(phone: str, note_id: int):
    """Delete a note."""
    from app.store.database import get_db
    async with get_db() as db:
        await db.execute(
            "DELETE FROM customer_notes WHERE id = ? AND phone = ?",
            (note_id, phone),
        )
        await db.commit()
    return {"status": "ok"}


# ── Follow-up Reminder ────────────────────────────────────────────

@router.post("/api/v1/customer/{phone}/followup", dependencies=[Depends(verify_admin)])
async def set_followup(phone: str, payload: dict):
    """Set or clear follow-up date. Body: {"date": "2026-03-10"} or {"date": ""}"""
    from app.store.database import get_db
    from app.store.audit import log_action

    date_str = (payload.get("date") or "").strip()
    async with get_db() as db:
        await db.execute(
            "UPDATE conversations SET next_followup = ? WHERE phone = ?",
            (date_str, phone),
        )
        await db.commit()

    if date_str:
        await log_action("set_followup", phone, date_str)
    else:
        await log_action("clear_followup", phone)
    return {"status": "ok", "next_followup": date_str}


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
