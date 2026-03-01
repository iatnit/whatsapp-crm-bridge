"""Feishu Bot event callback router."""

import asyncio
import logging
import time
from collections import OrderedDict

from fastapi import APIRouter, Request

from app.config import settings
from app.writers.feishu_writer import _get_tenant_token, _get_http, BASE_URL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/feishu", tags=["feishu-bot"])

# ── Event dedup (OrderedDict, 5 min TTL) ─────────────────────────────

_seen_events: OrderedDict[str, float] = OrderedDict()
_DEDUP_TTL = 300  # 5 minutes


def _is_duplicate(event_id: str) -> bool:
    """Check if event_id was already processed. Evict stale entries."""
    now = time.time()
    # Evict old entries from the front
    while _seen_events:
        oldest_id, oldest_ts = next(iter(_seen_events.items()))
        if now - oldest_ts > _DEDUP_TTL:
            _seen_events.pop(oldest_id)
        else:
            break
    if event_id in _seen_events:
        return True
    _seen_events[event_id] = now
    return False


# ── Reply helper ─────────────────────────────────────────────────────

async def _reply_to_feishu(chat_id: str, text: str) -> bool:
    """Send a text message to a Feishu chat via the IM API."""
    try:
        token = await _get_tenant_token()
        http = _get_http()
        url = f"{BASE_URL}/im/v1/messages?receive_id_type=chat_id"
        import json
        resp = await http.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("Feishu reply failed: %s", data)
            return False
        return True
    except Exception as e:
        logger.exception("Feishu reply error: %s", e)
        return False


# ── Background processing ────────────────────────────────────────────

async def _process_message(chat_id: str, user_text: str, open_id: str):
    """Background task: run agent and reply."""
    try:
        from app.feishu_bot.agent import handle_message
        reply = await handle_message(user_text)
        await _reply_to_feishu(chat_id, reply)
    except Exception as e:
        logger.exception("Feishu bot processing error: %s", e)
        await _reply_to_feishu(chat_id, f"处理出错: {e}")


# ── Event endpoint ───────────────────────────────────────────────────

@router.post("/event")
async def feishu_event(request: Request):
    """Handle Feishu event callback.

    1. Challenge verification
    2. Event dedup
    3. Extract text / chat_id / open_id
    4. Whitelist check
    5. Background processing (3s ACK)
    """
    body = await request.json()

    # 1) Challenge verification (app setup handshake)
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    # Verify token if configured
    token = body.get("token", "")
    if settings.feishu_bot_verification_token and token != settings.feishu_bot_verification_token:
        logger.warning("Feishu event: invalid verification token")
        return {"code": 0}

    # Check master switch
    if not settings.feishu_bot_enabled:
        return {"code": 0}

    # 2) Event dedup
    header = body.get("header", {})
    event_id = header.get("event_id", "")
    if event_id and _is_duplicate(event_id):
        logger.debug("Feishu event dedup: %s", event_id)
        return {"code": 0}

    # 3) Extract message fields
    event = body.get("event", {})
    message = event.get("message", {})
    chat_id = message.get("chat_id", "")
    sender = event.get("sender", {})
    open_id = sender.get("sender_id", {}).get("open_id", "")

    # Only handle text messages
    msg_type = message.get("message_type", "")
    if msg_type != "text":
        logger.debug("Feishu bot: ignoring non-text message type=%s", msg_type)
        return {"code": 0}

    # Parse text content
    import json
    try:
        content = json.loads(message.get("content", "{}"))
        user_text = content.get("text", "").strip()
    except (json.JSONDecodeError, TypeError):
        user_text = ""

    if not user_text or not chat_id:
        return {"code": 0}

    # 4) Whitelist check
    allowed = {
        uid.strip()
        for uid in settings.feishu_bot_allowed_users.split(",")
        if uid.strip()
    }
    if allowed and open_id not in allowed:
        logger.warning("Feishu bot: unauthorized user %s", open_id)
        return {"code": 0}

    logger.info("Feishu bot msg from %s: %s", open_id, user_text[:80])

    # 5) Background processing — return immediately for 3s ACK
    asyncio.create_task(_process_message(chat_id, user_text, open_id))

    return {"code": 0}
