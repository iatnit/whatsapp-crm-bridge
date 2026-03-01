"""WATI webhook endpoint — receives all WhatsApp messages (inbound + outbound)."""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request

from app.webhook.media import download_media
from app.store.messages import save_message, update_conversation
from app.autoreply.responder import handle_auto_reply, notify_outbound

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


async def _hubspot_upsert_contact(phone: str, display_name: str) -> None:
    """Fire-and-forget HubSpot contact upsert with first_contact_date.

    Also stores the HubSpot contact ID in the conversations table (P3 sync).
    Note: feishu编号 → HubSpot sync happens in the daily pipeline, not here,
    because the parallel Feishu task may not have finished yet.
    """
    from app.config import settings
    if not settings.hubspot_enabled:
        return
    try:
        from app.writers.hubspot_writer import ensure_contact
        from app.store.conversations import update_hubspot_id
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        contact_id = await ensure_contact(
            phone, name=display_name,
            extra={"first_contact_date": today, "last_contact_date": today},
        )
        if contact_id:
            await update_hubspot_id(phone, contact_id)
    except Exception as e:
        logger.error("HubSpot upsert failed: %s", e)


async def _feishu_ensure_customer(phone: str, display_name: str) -> None:
    """Fire-and-forget Feishu customer creation on first inbound message.

    Also stores the Feishu record_id in the conversations table (P3 sync).
    """
    try:
        from app.writers.feishu_writer import ensure_customer
        from app.store.conversations import update_customer_match

        record_id = await ensure_customer(
            display_name, phone=phone, contact_person=display_name,
        )
        if record_id:
            await update_customer_match(phone, record_id, display_name, "auto_created")
    except Exception as e:
        logger.error("Feishu real-time customer creation failed: %s", e)


@router.post("/webhook")
async def receive_webhook(request: Request):
    """Process incoming webhook events from WATI.

    WATI sends a flat JSON for each message event.
    Key fields:
      - waId: sender phone number
      - senderName: WhatsApp display name
      - text: message content
      - type: text/image/document/audio/video/sticker/location/contacts
      - owner: false=inbound (customer), true=outbound (us)
      - timestamp: unix timestamp string
      - id / whatsappMessageId: message ID for dedup
      - data: extra payload for media messages
    """
    try:
        payload: dict[str, Any] = await request.json()
    except Exception as e:
        logger.error("Invalid webhook JSON: %s", e)
        return {"status": "error", "message": "Invalid JSON"}

    # WATI may send different eventTypes; we only care about messages
    event_type = payload.get("eventType", "")
    if event_type != "message":
        logger.debug("Ignoring non-message event: %s", event_type)
        return {"status": "ignored"}

    wa_message_id = payload.get("whatsappMessageId", "") or payload.get("id", "")
    phone = payload.get("waId", "")
    display_name = payload.get("senderName", "")
    is_outbound = payload.get("owner", False)
    direction = "outbound" if is_outbound else "inbound"
    msg_type = payload.get("type", "text")
    text = payload.get("text", "")
    timestamp = int(payload.get("timestamp", "0") or "0")

    # Extract content based on message type
    content = ""
    media_path = ""

    if msg_type == "text":
        content = text
    elif msg_type in ("image", "video", "audio", "document", "voice"):
        content = text or f"[{msg_type}]"
        # WATI puts media URL in data or sourceUrl
        media_url = payload.get("sourceUrl") or ""
        if not media_url:
            data_obj = payload.get("data")
            if isinstance(data_obj, dict):
                media_url = data_obj.get("url", "")
        if media_url:
            media_path = await download_media(
                wa_message_id, media_url,
                display_name=display_name, phone=phone,
            ) or ""
    elif msg_type == "sticker":
        content = "[sticker]"
    elif msg_type == "location":
        content = text or "[location]"
    elif msg_type == "contacts":
        content = "[contact card]"
    elif msg_type == "reaction":
        content = text or "[reaction]"
    elif msg_type in ("button", "interactive"):
        # Interactive button/list replies
        button_reply = payload.get("interactiveButtonReply") or payload.get("buttonReply")
        if isinstance(button_reply, dict):
            content = button_reply.get("title", "") or button_reply.get("id", "")
        list_reply = payload.get("listReply")
        if isinstance(list_reply, dict):
            content = list_reply.get("title", "") or text
        if not content:
            content = text or f"[{msg_type}]"
    else:
        content = text or f"[{msg_type}]"

    if not phone:
        logger.warning("Webhook payload missing waId, skipping")
        return {"status": "skipped"}

    inserted = await save_message(
        wa_message_id=wa_message_id,
        phone=phone,
        display_name=display_name,
        direction=direction,
        msg_type=msg_type,
        content=content,
        media_path=media_path,
        timestamp=timestamp,
    )
    if inserted:
        await update_conversation(phone, display_name)
        logger.info(
            "%s %s (%s): %s",
            direction.upper(),
            display_name or phone,
            msg_type,
            content[:80],
        )

        # Track human outbound → pause AI auto-reply
        if direction == "outbound":
            notify_outbound(phone)

        # Real-time CRM upsert (fire-and-forget)
        if direction == "inbound" and msg_type not in ("reaction", "sticker"):
            asyncio.create_task(_hubspot_upsert_contact(phone, display_name))
            asyncio.create_task(_feishu_ensure_customer(phone, display_name))

        # Trigger AI auto-reply for inbound messages
        if direction == "inbound" and msg_type not in ("reaction", "sticker"):
            asyncio.create_task(
                handle_auto_reply(phone, display_name, content, msg_type)
            )

    return {"status": "ok"}
