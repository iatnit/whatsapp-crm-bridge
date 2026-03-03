"""Sync Feishu 客户跟进记录 → HubSpot Notes.

Runs on a schedule (every 30 min). Uses a timestamp watermark stored in
data/feishu_hs_sync.json to fetch only new records since last run.

Flow per record:
  1. Extract customer name from 客户名称 linked field
  2. Look up phone via Feishu 客户管理CRM by customer name (cached per run)
  3. Find or create HubSpot contact by phone / name
  4. Create a HubSpot Note with the followup content
  5. Advance watermark
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_STATE_FILE = Path("data/feishu_hs_sync.json")
_FEISHU_BASE = "https://open.feishu.cn/open-apis"

# ── State management ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_sync_ms": 0, "synced_ids": []}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep only last 2000 synced IDs to bound file size
    state["synced_ids"] = state.get("synced_ids", [])[-2000:]
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ── Feishu helpers ────────────────────────────────────────────────────────────

async def _feishu_token() -> str:
    """Get Feishu tenant access token."""
    from app.writers.feishu_writer import _get_tenant_token
    return await _get_tenant_token()


async def _fetch_new_followups(since_ms: int) -> list[dict]:
    """Page through all followup records and return those newer than since_ms.

    Feishu date-field server-side filtering is unreliable, so we fetch all
    pages and filter client-side. With ~1000 records this takes ~15 s which
    is acceptable for a 30-min scheduled job.
    """
    if not settings.feishu_app_token or not settings.feishu_table_followup:
        return []

    token = await _feishu_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = (
        f"{_FEISHU_BASE}/bitable/v1/apps/{settings.feishu_app_token}"
        f"/tables/{settings.feishu_table_followup}/records/search"
    )

    # Feishu date field does not support server-side filtering.
    # Use max page_size (500) to minimise API calls: 40k records / 500 = ~80 pages × 1.2s ≈ 96s.
    all_items: list[dict] = []
    page_token = ""

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            payload: dict = {"page_size": 500}
            if page_token:
                payload["page_token"] = page_token
            # Retry up to 3 times on transient network errors
            for attempt in range(3):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    break
                except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
                    if attempt == 2:
                        raise
                    logger.warning("Feishu request failed (attempt %d/3): %s — retrying", attempt + 1, e)
                    await asyncio.sleep(3 * (attempt + 1))
            data = resp.json()
            if data.get("code") != 0:
                logger.error("Feishu followup search error: %s", data.get("msg"))
                break
            all_items.extend(data.get("data", {}).get("items", []))
            if not data.get("data", {}).get("has_more"):
                break
            page_token = data.get("data", {}).get("page_token", "")

    results = [
        item for item in all_items
        if int(item.get("fields", {}).get("跟进时间") or 0) > since_ms
    ]
    logger.info(
        "Feishu: scanned %d records, %d new since %d",
        len(all_items), len(results), since_ms,
    )
    return results


async def _get_customer_phone(customer_name: str, cache: dict) -> str:
    """Look up phone number for a customer name, using an in-run cache."""
    if customer_name in cache:
        return cache[customer_name]
    phone = ""
    if customer_name:
        try:
            from app.writers.feishu_writer import _search_records
            items = await _search_records(
                table_id=settings.feishu_table_customers,
                field_name="客户",
                value=customer_name,
            )
            if items:
                raw = items[0].get("fields", {}).get("联系电话", "") or ""
                if isinstance(raw, list):
                    raw = raw[0] if raw else ""
                phone = str(raw).strip()
        except Exception as e:
            logger.debug("Phone lookup failed for %s: %s", customer_name, e)
    cache[customer_name] = phone
    return phone


def _extract_customer_name(field_value) -> str:
    """Extract customer name string from Feishu DuplexLink field."""
    if isinstance(field_value, list) and field_value:
        item = field_value[0]
        if isinstance(item, dict):
            return item.get("text", "") or ""
    if isinstance(field_value, str):
        return field_value
    return ""


# ── Main sync ─────────────────────────────────────────────────────────────────

async def sync_feishu_to_hubspot() -> int:
    """Sync new Feishu followup records to HubSpot Notes.

    Returns count of notes created.
    """
    try:
        return await _sync_feishu_to_hubspot_inner()
    except Exception as e:
        logger.error("Feishu→HubSpot sync failed: %s", e, exc_info=True)
        return 0


async def _sync_feishu_to_hubspot_inner() -> int:
    if not settings.hubspot_enabled or not settings.hubspot_access_token:
        return 0
    if not settings.feishu_app_token:
        return 0

    state = _load_state()
    since_ms = state.get("last_sync_ms", 0)
    synced_ids: set = set(state.get("synced_ids", []))

    # First run: initialise watermark to 7 days ago to avoid full history backfill
    if since_ms == 0:
        since_ms = int((time.time() - 7 * 86400) * 1000)
        state["last_sync_ms"] = since_ms
        _save_state(state)
        logger.info("First run: watermark initialised to 7 days ago (%d)", since_ms)

    records = await _fetch_new_followups(since_ms)
    if not records:
        logger.info("Feishu→HubSpot sync: no new followup records")
        return 0

    from app.writers.hubspot_writer import ensure_contact, create_note

    created = 0
    max_ts = since_ms
    phone_cache: dict[str, str] = {}  # customer_name → phone, avoids duplicate lookups

    for record in records:
        record_id = record.get("record_id", "")
        if not record_id or record_id in synced_ids:
            continue

        fields = record.get("fields", {})
        followup_ts = int(fields.get("跟进时间") or 0)
        max_ts = max(max_ts, followup_ts)

        # Extract fields
        customer_name = _extract_customer_name(fields.get("客户名称", ""))
        title = str(fields.get("跟进内容", "") or "").strip()
        detail = str(fields.get("跟进情况", "") or "").strip()
        summary = str(fields.get("总结", "") or "").strip()
        method = str(fields.get("跟进形式", "") or "WhatsApp沟通").strip()

        if not customer_name or not (title or detail):
            synced_ids.add(record_id)
            continue

        logger.info("Syncing followup [%s] %s → HubSpot", record_id[:8], customer_name)

        # Get phone (cached)
        phone = await _get_customer_phone(customer_name, phone_cache)

        # Find / create HubSpot contact
        try:
            contact_id = await ensure_contact(
                phone=phone,
                name=customer_name,
                country="",
                extra={},
            )
        except Exception as e:
            logger.warning("HubSpot contact lookup failed for %s: %s", customer_name, e)
            continue

        if not contact_id:
            logger.warning("No HubSpot contact for %s, skipping", customer_name)
            continue

        # Create HubSpot note
        note_title = f"[飞书跟进] {title or customer_name}"
        note_body = f"**跟进形式**: {method}\n\n{detail or title}"
        try:
            note_id = await create_note(
                contact_id=contact_id,
                title=note_title,
                body=note_body,
                summary=summary,
            )
            if note_id:
                logger.info("HubSpot note %s created for %s", note_id, customer_name)
                synced_ids.add(record_id)
                created += 1
        except Exception as e:
            logger.error("HubSpot note failed for %s: %s", customer_name, e)

    # Save updated state
    state["last_sync_ms"] = max_ts if max_ts > since_ms else since_ms
    state["synced_ids"] = list(synced_ids)
    _save_state(state)

    logger.info("Feishu→HubSpot sync complete: %d notes created", created)
    return created
