"""Periodic sync of outbound (agent-sent) messages from WATI API.

WATI webhooks only fire for inbound messages in most configurations.
This module polls the WATI getMessages API every few minutes to capture
outbound replies sent by Lucky from the WATI dashboard or phone, and
writes them to the local DB + forwards to the Obsidian receiver.
"""

import logging
import time

import httpx

from app.config import settings
from app.store.database import get_db
from app.store.messages import save_message, update_conversation

logger = logging.getLogger(__name__)

# How far back to look for new messages on each sync (seconds)
_LOOKBACK_SECONDS = 600  # 10 minutes (covers 2× the 5-min interval)


async def _get_active_phones() -> list[dict]:
    """Return conversations active in the last 7 days."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT phone, display_name, customer_name
            FROM conversations
            WHERE last_message_at >= datetime('now', '-7 days')
            ORDER BY last_message_at DESC
            """,
        )
        return [dict(row) for row in await cursor.fetchall()]


async def _fetch_wati_messages(phone: str) -> list[dict]:
    """Call WATI getMessages API for the given phone, return raw items."""
    url = f"{settings.wati_v1_url}/api/v1/getMessages/{phone}?pageSize=50"
    headers = {"Authorization": f"Bearer {settings.wati_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.debug("WATI getMessages %s → HTTP %d", phone, resp.status_code)
            return []
        return resp.json().get("messages", {}).get("items", []) or []
    except Exception as e:
        logger.warning("WATI getMessages failed for %s: %s", phone, e)
        return []


async def _sync_phone(phone: str, display_name: str, customer_name: str) -> int:
    """Sync outbound messages for one phone. Returns count of new messages saved."""
    items = await _fetch_wati_messages(phone)
    cutoff = int(time.time()) - _LOOKBACK_SECONDS
    saved = 0

    for item in items:
        # Only process outbound (owner=True) messages within the lookback window
        if not item.get("owner"):
            continue

        ts = int(float(item.get("timestamp") or 0))
        if ts < cutoff:
            continue

        msg_id = item.get("id", "")
        if not msg_id:
            continue

        msg_type = item.get("type") or "text"
        content = item.get("text") or ""

        # For non-text types, content may be empty — use type label
        if not content and msg_type != "text":
            content = f"[{msg_type}]"

        inserted = await save_message(
            wa_message_id=msg_id,
            phone=phone,
            display_name=display_name,
            direction="outbound",
            msg_type=msg_type,
            content=content,
            media_path="",
            timestamp=ts,
        )

        if inserted:
            await update_conversation(phone, display_name)
            # Forward to Obsidian local receiver
            try:
                from app.writers.obsidian_forwarder import forward_to_obsidian
                await forward_to_obsidian(
                    wa_message_id=msg_id,
                    phone=phone,
                    display_name=display_name,
                    customer_name=customer_name,
                    direction="outbound",
                    msg_type=msg_type,
                    content=content,
                    timestamp=ts,
                )
            except Exception as e:
                logger.warning("Obsidian forward failed for outbound %s: %s", msg_id, e)
            saved += 1

    return saved


async def sync_outbound_messages() -> None:
    """Scheduled job: sync recent outbound messages across all active conversations."""
    if not settings.wati_api_token:
        return

    conversations = await _get_active_phones()
    if not conversations:
        return

    total = 0
    for conv in conversations:
        count = await _sync_phone(
            phone=conv["phone"],
            display_name=conv.get("display_name", ""),
            customer_name=conv.get("customer_name", ""),
        )
        total += count

    if total:
        logger.info("Outbound sync: saved %d new message(s) across %d conversation(s)",
                    total, len(conversations))
