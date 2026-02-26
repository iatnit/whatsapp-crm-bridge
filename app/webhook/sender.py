"""Send outbound WhatsApp messages via WATI API and record them in DB."""

import logging
import time

import httpx

from app.config import settings
from app.store.messages import save_message, update_conversation

logger = logging.getLogger(__name__)


async def send_text_message(to: str, text: str) -> str | None:
    """Send a text message via WATI V3 API and save it to the database.

    Args:
        to: Recipient phone number (with country code, no +).
        text: Message text.

    Returns:
        The WATI message ID on success, None on failure.
    """
    url = f"{settings.wati_v3_url}/api/ext/v3/conversations/messages/text"
    headers = {
        "Authorization": f"Bearer {settings.wati_api_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "target": to,
        "text": text,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code not in (200, 201):
        logger.error("WATI send failed to %s: HTTP %d %s", to, resp.status_code, resp.text)
        return None

    data = resp.json() if resp.text else {}

    # Build message ID from response or generate one
    wa_message_id = data.get("id", "") or f"out-{to}-{int(time.time())}"

    # Record outbound message in DB
    await save_message(
        wa_message_id=wa_message_id,
        phone=to,
        display_name="",
        direction="outbound",
        msg_type="text",
        content=text,
        media_path="",
        timestamp=int(time.time()),
    )
    await update_conversation(to)
    logger.info("Sent via WATI to %s: %s", to, text[:80])
    return wa_message_id
