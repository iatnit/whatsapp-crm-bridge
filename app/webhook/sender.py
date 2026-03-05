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

# ── Shared HTTP client (TCP connection reuse) ─────────────────────────
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    """Get or create shared httpx client for TCP connection reuse."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=15)
    return _http


async def close_http_client():
    """Close the shared httpx client on shutdown."""
    global _http
    if _http and not _http.is_closed:
        await _http.aclose()
        _http = None


async def send_template_message(
    to: str,
    template_name: str,
    parameters: list[str] | None = None,
    broadcast_name: str = "dormant_outreach",
) -> str | None:
    """Send a WhatsApp template message via WATI V1 API.

    Template messages bypass the 24-hour window restriction, making them
    suitable for re-engaging dormant customers.

    Args:
        to: Recipient phone number (with country code, no +).
        template_name: Approved WATI template name (from WATI dashboard).
        parameters: List of parameter values for template placeholders.
        broadcast_name: Label for the broadcast (for WATI analytics).

    Returns:
        WATI message ID on success, None on failure.
    """
    if not settings.wati_api_token:
        return None

    url = f"{settings.wati_v1_url}/api/v1/sendTemplateMessage"
    headers = {
        "Authorization": f"Bearer {settings.wati_api_token}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "template_name": template_name,
        "broadcast_name": broadcast_name,
        "parameters": [
            {"name": str(i + 1), "value": v}
            for i, v in enumerate(parameters or [])
        ],
    }

    try:
        client = _get_http()
        resp = await client.post(
            url,
            params={"whatsappNumber": to},
            json=payload,
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            logger.error(
                "WATI template send failed to %s: HTTP %d %s",
                to, resp.status_code, resp.text[:200],
            )
            return None

        data = resp.json() if resp.text else {}
        wa_message_id = (
            data.get("messageId") or data.get("id") or
            f"tmpl-{to}-{int(time.time() * 1000)}"
        )

        await save_message(
            wa_message_id=wa_message_id,
            phone=to,
            display_name="",
            direction="outbound",
            msg_type="template",
            content=f"[Template: {template_name}]",
            media_path="",
            timestamp=int(time.time()),
        )
        await update_conversation(to)
        logger.info("Template '%s' sent to %s (id=%s)", template_name, to, wa_message_id)
        return wa_message_id

    except Exception as e:
        logger.error("WATI template send error for %s: %s", to, e)
        return None


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
            client = _get_http()
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
