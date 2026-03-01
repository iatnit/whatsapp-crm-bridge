"""Forward WhatsApp messages to local Obsidian receiver via HMAC-signed POST."""

import hashlib
import hmac
import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


def _sign(body: bytes) -> str:
    """Compute HMAC-SHA256 hex digest for request body."""
    return hmac.new(
        settings.obsidian_sync_secret.encode(), body, hashlib.sha256
    ).hexdigest()


async def forward_to_obsidian(
    *,
    wa_message_id: str,
    phone: str,
    display_name: str,
    customer_name: str,
    direction: str,
    msg_type: str,
    content: str,
    timestamp: int,
) -> None:
    """Fire-and-forget POST to the local Obsidian receiver.

    Failures are logged as warnings — never blocks the webhook.
    """
    if not settings.obsidian_sync_enabled or not settings.obsidian_sync_url:
        return

    payload = {
        "wa_message_id": wa_message_id,
        "phone": phone,
        "display_name": display_name,
        "customer_name": customer_name,
        "direction": direction,
        "msg_type": msg_type,
        "content": content,
        "timestamp": timestamp,
    }

    body = json.dumps(payload, ensure_ascii=False).encode()
    url = f"{settings.obsidian_sync_url.rstrip('/')}/api/v1/message"
    headers = {
        "Content-Type": "application/json",
        "X-Signature": f"hmac-sha256={_sign(body)}",
    }

    try:
        resp = await _get_client().post(url, content=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("Obsidian sync responded %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Obsidian sync failed (non-blocking): %s", e)


async def forward_summary_to_obsidian(
    *,
    customer_name: str,
    phone: str,
    display_name: str,
    feishu_id: str = "",
    location: str = "",
    language: str = "",
    summary: str = "",
    demand_summary: str = "",
    followup_title: str = "",
    followup_detail: str = "",
    recommended_codes: list[str] | None = None,
    next_actions: list[str] | None = None,
    tags: list[str] | None = None,
    date: str = "",
) -> None:
    """Fire-and-forget POST CRM summary to the local Obsidian receiver.

    Failures are logged as warnings — never blocks the pipeline.
    """
    if not settings.obsidian_sync_enabled or not settings.obsidian_sync_url:
        return

    payload = {
        "customer_name": customer_name,
        "phone": phone,
        "display_name": display_name,
        "feishu_id": feishu_id,
        "location": location,
        "language": language,
        "summary": summary,
        "demand_summary": demand_summary,
        "followup_title": followup_title,
        "followup_detail": followup_detail,
        "recommended_codes": recommended_codes or [],
        "next_actions": next_actions or [],
        "tags": tags or [],
        "date": date,
    }

    body = json.dumps(payload, ensure_ascii=False).encode()
    url = f"{settings.obsidian_sync_url.rstrip('/')}/api/v1/summary"
    headers = {
        "Content-Type": "application/json",
        "X-Signature": f"hmac-sha256={_sign(body)}",
    }

    try:
        resp = await _get_client().post(url, content=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("Obsidian summary sync responded %d: %s", resp.status_code, resp.text[:200])
        else:
            logger.info("Obsidian summary forwarded for %s", customer_name)
    except Exception as e:
        logger.warning("Obsidian summary sync failed (non-blocking): %s", e)


async def close_http_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
