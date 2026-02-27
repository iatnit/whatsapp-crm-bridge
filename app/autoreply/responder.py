"""Core auto-reply logic: rate limiting, LLM call, send reply."""

import asyncio
import logging
import time
from collections import defaultdict

import httpx

from app.config import settings
from app.store.messages import get_messages_by_phone
from app.webhook.sender import send_text_message
from app.autoreply.knowledge import get_knowledge_text
from app.autoreply.prompts import SYSTEM_PROMPT_TEMPLATE, USER_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)

# In-memory rate limiting state
_last_reply_ts: dict[str, float] = {}  # phone → last reply timestamp
_hourly_counts: dict[str, list[float]] = defaultdict(list)  # phone → list of reply timestamps
_ai_sent_ts: dict[str, float] = {}  # phone → timestamp when AI last sent (to distinguish from human)
_human_takeover: dict[str, float] = {}  # phone → timestamp when human took over


def _check_cooldown(phone: str) -> bool:
    """Return True if we should skip due to cooldown."""
    last = _last_reply_ts.get(phone, 0)
    return (time.time() - last) < settings.auto_reply_cooldown


def _check_hourly_limit(phone: str) -> bool:
    """Return True if hourly limit exceeded."""
    now = time.time()
    one_hour_ago = now - 3600
    # Clean old entries
    _hourly_counts[phone] = [ts for ts in _hourly_counts[phone] if ts > one_hour_ago]
    return len(_hourly_counts[phone]) >= settings.auto_reply_max_per_hour


def _check_human_takeover(phone: str) -> bool:
    """Return True if a human has taken over this conversation recently."""
    takeover_ts = _human_takeover.get(phone, 0)
    if takeover_ts == 0:
        return False
    elapsed = time.time() - takeover_ts
    if elapsed < settings.auto_reply_human_pause:
        return True
    # Expired, clean up
    _human_takeover.pop(phone, None)
    return False


def notify_outbound(phone: str) -> None:
    """Called by webhook router when an outbound message is received.

    If the outbound message was NOT sent by our AI (i.e. it's from a human
    or WATI KnowBot manual reply), activate human takeover pause.
    """
    now = time.time()
    ai_sent = _ai_sent_ts.get(phone, 0)
    # If AI sent to this phone within the last 15 seconds, this outbound
    # is likely the echo of our own AI message → ignore
    if (now - ai_sent) < 15:
        return
    # Otherwise, a human or external system sent this → pause AI
    _human_takeover[phone] = now
    logger.info("Human takeover detected for %s, AI paused for %ds", phone, settings.auto_reply_human_pause)


def _record_reply(phone: str) -> None:
    """Record that we sent a reply."""
    now = time.time()
    _last_reply_ts[phone] = now
    _hourly_counts[phone].append(now)
    _ai_sent_ts[phone] = now


def _format_conversation(messages: list[dict]) -> str:
    """Format message history into a readable conversation."""
    # messages come in DESC order from DB, reverse for chronological
    lines = []
    for msg in reversed(messages):
        sender = "LOCA" if msg["direction"] == "outbound" else "Customer"
        content = msg.get("content", "")
        if not content:
            content = f"[{msg.get('msg_type', 'unknown')}]"
        lines.append(f"[{sender}]: {content}")
    return "\n".join(lines)


async def _has_recent_outbound(phone: str) -> bool:
    """Check if there's a very recent outbound message (e.g. from KnowBot)."""
    messages = await get_messages_by_phone(phone, limit=3)
    if not messages:
        return False
    # messages are DESC by timestamp; check the most recent
    latest = messages[0]
    if latest["direction"] == "outbound":
        age = time.time() - latest["timestamp"]
        # If there's an outbound message within the last 10 seconds, KnowBot likely replied
        if age < 10:
            return True
    return False


async def _call_gemini(system_prompt: str, user_prompt: str) -> str | None:
    """Call Gemini API and return the reply text."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={settings.gemini_api_key}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": settings.auto_reply_max_tokens,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            logger.error("Gemini auto-reply error %d: %s", resp.status_code, resp.text[:300])
            return None

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            logger.error("Gemini auto-reply: no candidates")
            return None

        text = candidates[0]["content"]["parts"][0]["text"].strip()
        # Remove any markdown formatting that might slip through
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0].strip()

        return text

    except Exception as e:
        logger.error("Gemini auto-reply call failed: %s", e)
        return None


async def handle_auto_reply(
    phone: str,
    display_name: str,
    content: str,
    msg_type: str,
) -> None:
    """Main entry point: decide whether to reply and send AI response.

    Called as a background task from the webhook handler.
    """
    if not settings.auto_reply_enabled:
        return

    if not settings.gemini_api_key:
        logger.warning("Auto-reply enabled but GEMINI_API_KEY not set")
        return

    # Skip non-text-like messages that don't need replies
    if msg_type in ("reaction", "sticker"):
        return

    # Human takeover check — if a human replied recently, AI stays silent
    if _check_human_takeover(phone):
        logger.info("Human takeover active for %s, AI auto-reply paused", phone)
        return

    # Rate limiting checks
    if _check_cooldown(phone):
        logger.debug("Auto-reply cooldown active for %s, skipping", phone)
        return

    if _check_hourly_limit(phone):
        logger.debug("Auto-reply hourly limit reached for %s, skipping", phone)
        return

    # Delay to let KnowBot respond first (anti-duplicate)
    await asyncio.sleep(settings.auto_reply_delay)

    # Check if KnowBot already replied during our delay
    if await _has_recent_outbound(phone):
        logger.info("KnowBot already replied to %s, skipping auto-reply", phone)
        return

    # Load conversation context
    messages = await get_messages_by_phone(
        phone, limit=settings.auto_reply_context_messages
    )
    conversation_text = _format_conversation(messages)

    # Build prompts
    knowledge = get_knowledge_text()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(knowledge_base=knowledge)
    customer_name = display_name or phone
    user_prompt = USER_PROMPT_TEMPLATE.format(
        customer_name=customer_name,
        phone=phone,
        conversation_text=conversation_text,
    )

    # Call LLM
    reply_text = await _call_gemini(system_prompt, user_prompt)
    if not reply_text:
        logger.warning("No reply generated for %s", phone)
        return

    # Send reply via WATI
    msg_id = await send_text_message(phone, reply_text)
    if msg_id:
        _record_reply(phone)
        logger.info("Auto-replied to %s: %s", phone, reply_text[:80])
    else:
        logger.error("Failed to send auto-reply to %s", phone)
