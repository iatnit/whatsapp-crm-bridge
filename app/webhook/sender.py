"""Send outbound WhatsApp messages via WATI API and record them in DB."""

import asyncio
import logging
import time

import httpx

from app.config import settings
from app.store.messages import save_message, update_conversation

logger = logging.getLogger(__name__)

# Max retry attempts for transient WATI failures
_MAX_SEND_RETRIES = 2


async def send_text_message(to: str, text: str) -> str | None:
    """Send a text message via WATI V3 API and save it to the database.

    Retries up to 2 times on transient failures (5xx, timeout).

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

    resp = None
    for attempt in range(_MAX_SEND_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (200, 201):
                break
            # 4xx = client error, don't retry
            if 400 <= resp.status_code < 500:
                logger.error("WATI send failed to %s: HTTP %d %s", to, resp.status_code, resp.text)
                return None
            # 5xx = server error, retry
            logger.warning(
                "WATI send to %s failed (attempt %d/%d): HTTP %d",
                to, attempt + 1, _MAX_SEND_RETRIES + 1, resp.status_code,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning(
                "WATI send to %s failed (attempt %d/%d): %s",
                to, attempt + 1, _MAX_SEND_RETRIES + 1, e,
            )
        if attempt < _MAX_SEND_RETRIES:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff

    if resp is None or resp.status_code not in (200, 201):
        logger.error("WATI send to %s failed after %d attempts", to, _MAX_SEND_RETRIES + 1)
        return None

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        data = {}

    # Build message ID from response or generate one (ms precision to avoid collision)
    wa_message_id = data.get("id", "") or f"out-{to}-{int(time.time() * 1000)}"

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
