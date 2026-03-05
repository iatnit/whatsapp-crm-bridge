"""AI Manager UI & API routes with HubSpot contact cache."""

import json
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.auth import verify_admin
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ai-manager"])
limiter = Limiter(key_func=get_remote_address)

# ── HubSpot contact cache ────────────────────────────────────────────

_hubspot_cache: list[dict] | None = None
_HUBSPOT_CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "hubspot_contacts.json"

_VALID_TAGS = {"hot_lead", "vip", "repeat_buyer", "first_timer", "price_shopper", "risky", "agent_potential"}
_VALID_SIZES = {"big", "medium", "small", ""}


def _digits(phone: str) -> str:
    """Strip all non-digit chars for phone matching."""
    return re.sub(r"\D", "", phone or "")


def _load_hubspot_from_disk() -> list[dict] | None:
    try:
        if _HUBSPOT_CACHE_FILE.exists():
            data = json.loads(_HUBSPOT_CACHE_FILE.read_text())
            logger.info("Loaded %d HubSpot contacts from disk cache", len(data))
            return data
    except Exception:
        logger.warning("Failed to read HubSpot disk cache, will fetch from API")
    return None


def _save_hubspot_to_disk(contacts: list[dict]) -> None:
    try:
        _HUBSPOT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HUBSPOT_CACHE_FILE.write_text(json.dumps(contacts, ensure_ascii=False))
        logger.info("Saved %d HubSpot contacts to disk cache", len(contacts))
    except Exception:
        logger.warning("Failed to write HubSpot disk cache")


async def get_hubspot_contacts() -> list[dict]:
    """Return in-memory HubSpot contacts. Load from disk on first call."""
    global _hubspot_cache
    if _hubspot_cache is not None:
        return _hubspot_cache
    _hubspot_cache = _load_hubspot_from_disk() or []
    return _hubspot_cache


async def refresh_hubspot_contacts() -> list[dict]:
    """Pull fresh contacts from HubSpot API, update memory + disk."""
    global _hubspot_cache
    from app.writers.hubspot_writer import list_all_contacts
    _hubspot_cache = await list_all_contacts()
    _save_hubspot_to_disk(_hubspot_cache)
    return _hubspot_cache


# ── AI Manager page ──────────────────────────────────────────────────

_ai_manager_html: str | None = None


@router.get("/ai-manager", response_class=HTMLResponse)
async def ai_manager_page():
    """Serve the AI Manager single-page UI."""
    global _ai_manager_html
    if _ai_manager_html is None:
        _ai_manager_html = (
            Path(__file__).parent.parent / "static" / "ai-manager.html"
        ).read_text()
    return _ai_manager_html


# ── Customer list (merged local + HubSpot) ───────────────────────────

@router.get("/api/v1/ai/customers", dependencies=[Depends(verify_admin)])
async def list_ai_customers():
    """Return merged local + HubSpot customers for the manager UI."""
    from app.store.conversations import (
        get_all_conversations, _parse_first_message_ts, calc_relationship_stage,
    )

    convs = await get_all_conversations()
    hs_contacts = await get_hubspot_contacts()

    hs_by_phone: dict[str, dict] = {}
    for h in hs_contacts:
        for field in ("phone", "whatsapp_number"):
            key = _digits(h.get(field, ""))
            if key and len(key) >= 7:
                hs_by_phone[key] = h

    seen_hs_keys: set[str] = set()
    customers: list[dict] = []

    for c in convs:
        total = c.get("total_messages") or 0
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


# ── AI disable/enable ─────────────────────────────────────────────────

@router.post("/api/v1/ai/disable/{phone}", dependencies=[Depends(verify_admin)])
async def disable_ai(phone: str):
    """Disable AI auto-reply for a customer."""
    from app.store.conversations import set_ai_disabled
    found = await set_ai_disabled(phone, disabled=True)
    if not found:
        return {"error": f"Phone {phone} not found in conversations"}
    return {"status": "ok", "phone": phone, "ai_disabled": True}


@router.post("/api/v1/ai/enable/{phone}", dependencies=[Depends(verify_admin)])
async def enable_ai(phone: str):
    """Re-enable AI auto-reply for a customer."""
    from app.store.conversations import set_ai_disabled
    found = await set_ai_disabled(phone, disabled=False)
    if not found:
        return {"error": f"Phone {phone} not found in conversations"}
    return {"status": "ok", "phone": phone, "ai_disabled": False}


@router.get("/api/v1/ai/disabled", dependencies=[Depends(verify_admin)])
async def list_ai_disabled():
    """List all customers with AI auto-reply disabled."""
    from app.store.conversations import get_ai_disabled_list
    customers = await get_ai_disabled_list()
    return {"count": len(customers), "customers": customers}


# ── Customer size ─────────────────────────────────────────────────────

@router.post("/api/v1/ai/customer-size/{phone}", dependencies=[Depends(verify_admin)])
async def set_customer_size_api(phone: str, payload: dict):
    """Set customer size classification. Body: {"size": "big"}"""
    from app.store.conversations import set_customer_size
    size = payload.get("size", "")
    if size not in _VALID_SIZES:
        return JSONResponse({"error": f"Invalid size: {size}"}, status_code=400)
    found = await set_customer_size(phone, size)
    if not found:
        return JSONResponse({"error": f"Phone {phone} not found"}, status_code=404)
    return {"status": "ok", "phone": phone, "customer_size": size}


# ── Tags ──────────────────────────────────────────────────────────────

@router.post("/api/v1/ai/tags/{phone}", dependencies=[Depends(verify_admin)])
async def update_tags(phone: str, payload: dict):
    """Update customer_tags on the HubSpot contact. Body: {"tags": "hot_lead;vip"}"""
    global _hubspot_cache
    from app.writers.hubspot_writer import search_contact_by_phone, update_customer_tags

    tags_str = payload.get("tags", "")
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

    if _hubspot_cache:
        phone_digits = _digits(phone)
        for h in _hubspot_cache:
            for field in ("phone", "whatsapp_number"):
                if _digits(h.get(field) or "") == phone_digits:
                    h["customer_tags"] = tags_str
                    break
        _save_hubspot_to_disk(_hubspot_cache)
    return {"status": "ok", "phone": phone, "tags": tags_str}


# ── Refresh ───────────────────────────────────────────────────────────

@router.post("/api/v1/ai/refresh", dependencies=[Depends(verify_admin)])
@limiter.limit("3/minute")
async def refresh_cache(request: Request):
    """Pull fresh HubSpot contacts and update local cache."""
    t0 = time.time()
    contacts = await refresh_hubspot_contacts()
    elapsed = round(time.time() - t0, 1)
    return {"status": "ok", "count": len(contacts), "seconds": elapsed}
