"""WhatsApp Cloud API webhook endpoints."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request, Response

from app.config import settings
from app.webhook.signature import verify_signature
from app.webhook.media import download_media
from app.store.messages import save_message, update_conversation

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


# ---------- GET /webhook — Meta verification ----------

@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode", default=""),
    hub_verify_token: str = Query(alias="hub.verify_token", default=""),
    hub_challenge: str = Query(alias="hub.challenge", default=""),
):
    """Meta sends a GET to verify the webhook URL during setup."""
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token:
        logger.info("Webhook verified successfully")
        return Response(content=hub_challenge, media_type="text/plain")
    logger.warning("Webhook verification failed: mode=%s", hub_mode)
    return Response(content="Forbidden", status_code=403)


# ---------- POST /webhook — message ingestion ----------

@router.post("/webhook")
async def receive_webhook(request: Request):
    """Process incoming webhook events from Meta Cloud API.

    Handles two event types:
    - messages: inbound messages from customers
    - statuses: delivery status updates for outbound messages
    """
    body = await verify_signature(request, settings.meta_app_secret)
    payload: dict[str, Any] = json.loads(body)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # --- Inbound messages ---
            contacts = {
                c["wa_id"]: c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
            }

            for msg in value.get("messages", []):
                await _handle_inbound(msg, contacts)

            # --- Outbound status updates ---
            for status in value.get("statuses", []):
                await _handle_status(status)

    return {"status": "ok"}


async def _handle_inbound(msg: dict, contacts: dict[str, str]) -> None:
    """Process a single inbound message."""
    phone = msg.get("from", "")
    display_name = contacts.get(phone, "")
    wa_id = msg.get("id", "")
    ts = int(msg.get("timestamp", 0))
    msg_type = msg.get("type", "text")

    # Extract content based on message type
    content = ""
    media_path = ""

    if msg_type == "text":
        content = msg.get("text", {}).get("body", "")
    elif msg_type in ("image", "video", "audio", "document"):
        media_obj = msg.get(msg_type, {})
        content = media_obj.get("caption", "")
        media_id = media_obj.get("id")
        if media_id:
            media_path = await download_media(media_id) or ""
    elif msg_type == "sticker":
        content = "[sticker]"
    elif msg_type == "location":
        loc = msg.get("location", {})
        content = f"[location: {loc.get('latitude')},{loc.get('longitude')}]"
    elif msg_type == "contacts":
        content = "[contact card]"
    elif msg_type == "reaction":
        reaction = msg.get("reaction", {})
        content = f"[reaction: {reaction.get('emoji', '')}]"
    else:
        content = f"[{msg_type}]"

    inserted = await save_message(
        wa_message_id=wa_id,
        phone=phone,
        display_name=display_name,
        direction="inbound",
        msg_type=msg_type,
        content=content,
        media_path=media_path,
        timestamp=ts,
    )
    if inserted:
        await update_conversation(phone, display_name)
        logger.info("Inbound from %s (%s): %s", display_name or phone, msg_type, content[:80])


async def _handle_status(status: dict) -> None:
    """Process an outbound delivery status update.

    We log the status event but the actual outbound message content
    is recorded at send-time via the send_message() helper.
    """
    recipient = status.get("recipient_id", "")
    wa_id = status.get("id", "")
    status_val = status.get("status", "")  # sent / delivered / read / failed
    ts = int(status.get("timestamp", 0))

    logger.debug(
        "Outbound status: %s → %s at %d (%s)", wa_id, recipient, ts, status_val
    )
