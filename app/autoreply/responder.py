"""Core auto-reply logic: rate limiting, LLM call, send reply."""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from app.config import settings
from app.llm.gemini import call_gemini
from app.store.messages import get_messages_by_phone
from app.store.conversations import get_customer_context, is_ai_disabled
from app.webhook.sender import send_text_message
from app.autoreply.knowledge import get_knowledge_text, get_reply_style
from app.autoreply.prompts import SYSTEM_PROMPT_TEMPLATE, USER_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)

# ── Timezone awareness ─────────────────────────────────────────────────
# Phone country prefix → UTC offset in hours (covers 95%+ of LOCA customers)
_TIMEZONE_MAP: dict[str, float] = {
    "+91": 5.5,    # India
    "+92": 5.0,    # Pakistan
    "+94": 5.5,    # Sri Lanka
    "+977": 5.75,  # Nepal
    "+880": 6.0,   # Bangladesh
    "+60": 8.0,    # Malaysia
    "+62": 7.0,    # Indonesia (WIB)
    "+63": 8.0,    # Philippines
    "+66": 7.0,    # Thailand
    "+84": 7.0,    # Vietnam
    "+95": 6.5,    # Myanmar
    "+971": 4.0,   # UAE
    "+966": 3.0,   # Saudi Arabia
    "+973": 3.0,   # Bahrain
    "+974": 3.0,   # Qatar
    "+968": 4.0,   # Oman
    "+965": 3.0,   # Kuwait
    "+962": 2.0,   # Jordan
    "+964": 3.0,   # Iraq
    "+961": 2.0,   # Lebanon
    "+20": 2.0,    # Egypt
    "+234": 1.0,   # Nigeria
    "+254": 3.0,   # Kenya
    "+27": 2.0,    # South Africa
    "+44": 0.0,    # UK
    "+33": 1.0,    # France
    "+49": 1.0,    # Germany
    "+39": 1.0,    # Italy
    "+34": 1.0,    # Spain
    "+351": 0.0,   # Portugal
    "+55": -3.0,   # Brazil
    "+1": -5.0,    # USA/Canada (EST approx)
    "+86": 8.0,    # China
}


def _get_customer_local_hour(phone: str) -> int | None:
    """Return customer's current local hour (0-23), or None if timezone unknown."""
    # Match longest prefix first
    for prefix in sorted(_TIMEZONE_MAP, key=len, reverse=True):
        if phone.startswith(prefix):
            offset = _TIMEZONE_MAP[prefix]
            tz = timezone(timedelta(hours=offset))
            return datetime.now(tz).hour
    return None


def _is_sleeping_hours(phone: str) -> bool:
    """Return True if it's 1am–6am in the customer's local timezone (likely asleep).

    Outside these hours → allow auto-reply as usual.
    Unknown timezone → allow (default safe).
    """
    hour = _get_customer_local_hour(phone)
    if hour is None:
        return False  # unknown → don't suppress
    return 1 <= hour < 6


# In-memory rate limiting state
_last_reply_ts: dict[str, float] = {}  # phone → last reply timestamp
_hourly_counts: dict[str, list[float]] = defaultdict(list)  # phone → list of reply timestamps
_ai_sent_ts: dict[str, float] = {}  # phone → timestamp when AI last sent (to distinguish from human)
_human_takeover: dict[str, float] = {}  # phone → timestamp when human took over
_phone_locks: dict[str, asyncio.Lock] = {}  # phone → lock (prevent concurrent replies)
_last_reply_text: dict[str, str] = {}  # phone → last reply text (prevent duplicate content)
_human_active_history: dict[str, float] = {}  # phone → last time human outbound was detected
_last_cleanup: float = 0  # timestamp of last memory cleanup


def _cleanup_stale_entries() -> None:
    """Remove entries older than 24h from all in-memory dicts.

    Prevents unbounded growth when handling thousands of unique phone numbers.
    Called at most once per hour.
    """
    global _last_cleanup
    now = time.time()
    if (now - _last_cleanup) < 3600:
        return
    _last_cleanup = now

    cutoff = now - 86400  # 24 hours ago
    stale_count = 0

    for cache in (_last_reply_ts, _ai_sent_ts, _human_takeover, _human_active_history):
        stale = [k for k, v in cache.items() if v < cutoff]
        for k in stale:
            del cache[k]
        stale_count += len(stale)

    # _hourly_counts: remove entries with all timestamps expired
    stale_hourly = [k for k, v in _hourly_counts.items() if not v or max(v) < cutoff]
    for k in stale_hourly:
        del _hourly_counts[k]
    stale_count += len(stale_hourly)

    # _phone_locks: remove unlocked locks for stale phones
    stale_locks = [
        k for k in _phone_locks
        if k not in _last_reply_ts and not _phone_locks[k].locked()
    ]
    for k in stale_locks:
        del _phone_locks[k]
    stale_count += len(stale_locks)

    # _last_reply_text: remove for phones with no recent activity
    stale_text = [k for k in _last_reply_text if k not in _last_reply_ts]
    for k in stale_text:
        del _last_reply_text[k]
    stale_count += len(stale_text)

    if stale_count:
        logger.info("Auto-reply memory cleanup: removed %d stale entries", stale_count)


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
    # If AI sent to this phone within the last 120 seconds, this outbound
    # is likely the echo of our own AI message → ignore
    # WATI can take up to 60-90s to deliver outbound webhooks; use 120s to be safe.
    if (now - ai_sent) < 120:
        return
    # Otherwise, a human or external system sent this → pause AI
    _human_takeover[phone] = now
    _human_active_history[phone] = now
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


def _format_customer_context(ctx: dict) -> str:
    """Convert customer context dict to a concise text block for the prompt."""
    stage = ctx["relationship_stage"]
    total = ctx["total_messages"]
    days = ctx["first_seen_days"]
    tier = ctx.get("customer_tier", "")
    product_interest = ctx.get("product_interest", "")

    lines = [f"CUSTOMER CONTEXT: stage={stage}, messages={total}, days={days}"]
    if ctx["is_known"] and ctx["customer_name"]:
        lines.append(f"CRM name: {ctx['customer_name']} (known customer)")

    # Tier context — affects how attentive and proactive Lucky should be
    if tier == "S":
        lines.append("Tier: S ⭐⭐⭐⭐⭐ — TOP customer (highest revenue). Be extra responsive and warm. Prioritize this conversation.")
    elif tier == "A":
        lines.append("Tier: A ⭐⭐⭐⭐ — Key account. Important customer, treat with care.")
    elif tier == "B":
        lines.append("Tier: B ⭐⭐⭐ — Good regular customer.")
    elif tier in ("C", "D"):
        lines.append(f"Tier: {tier} — Smaller customer. Refer to local agent if quantity is below MOQ.")

    # Product interest from CRM
    if product_interest:
        products = product_interest.replace(";", ", ")
        lines.append(f"Known product interest: {products}")

    # Relationship guidance
    if stage == "new" and total == 1:
        lines.append("→ FIRST EVER message. Welcome them briefly, then ask ONE of: which product? or do you have shop/factory? Do NOT send a long company intro.")
    elif stage == "new":
        lines.append("→ Very new contact. Ask basic questions (product interest, shop/factory). Keep it casual.")
    elif stage == "early":
        lines.append("→ Early contact. Check history before asking — don't repeat questions.")
    elif stage == "developing":
        lines.append("→ Developing relationship. Skip introductions, be direct.")
    else:
        lines.append("→ Established customer. Treat as a familiar friend.")
    return "\n".join(lines)


async def _has_recent_outbound(phone: str, window: int = 10) -> bool:
    """Check if there's ANY outbound message within `window` seconds.

    Scans the last 30 messages (not just the latest) so we don't miss
    an outbound reply buried under newer inbound messages.
    """
    messages = await get_messages_by_phone(phone, limit=30)
    if not messages:
        return False
    now = time.time()
    for msg in messages:
        if (now - msg["timestamp"]) > window:
            break  # messages are DESC by timestamp, older ones won't match
        if msg["direction"] == "outbound":
            return True
    return False


async def _call_anthropic_reply(system_prompt: str, user_prompt: str) -> str | None:
    """Fallback: call Anthropic Claude for auto-reply when Gemini fails."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=30)
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=settings.auto_reply_max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error("Anthropic auto-reply fallback failed: %s", e)
        return None


async def _call_gemini_reply(system_prompt: str, user_prompt: str) -> str | None:
    """Call Gemini API for auto-reply (text mode, shorter output)."""
    text = await call_gemini(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=settings.auto_reply_max_tokens,
        timeout=30,
    )
    if not text:
        return None
    # Remove any markdown formatting that might slip through
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0].strip()
    return text


_MAX_PHONE_LOCKS = 5000


def _get_phone_lock(phone: str) -> asyncio.Lock:
    """Get or create a per-phone lock to prevent concurrent replies."""
    if phone not in _phone_locks:
        # Evict stale unlocked entries if at capacity
        if len(_phone_locks) >= _MAX_PHONE_LOCKS:
            stale = [k for k, v in _phone_locks.items() if not v.locked()]
            for k in stale[:1000]:
                del _phone_locks[k]
        _phone_locks[phone] = asyncio.Lock()
    return _phone_locks[phone]


def _is_duplicate_reply(phone: str, text: str) -> bool:
    """Check if the reply text is the same as the last one sent to this phone."""
    last = _last_reply_text.get(phone, "")
    if not last:
        return False
    # Exact match or very similar (one is substring of the other)
    if text == last:
        return True
    shorter, longer = (text, last) if len(text) < len(last) else (last, text)
    if len(shorter) > 10 and shorter in longer:
        return True
    return False


async def handle_auto_reply(
    phone: str,
    display_name: str,
    content: str,
    msg_type: str,
) -> None:
    """Main entry point: decide whether to reply and send AI response.

    Called as a background task from the webhook handler.
    Uses per-phone lock to prevent concurrent duplicate replies.
    """
    if not settings.auto_reply_enabled:
        return

    # Periodic cleanup of stale in-memory entries (at most once/hour)
    _cleanup_stale_entries()

    if not settings.gemini_api_key:
        logger.warning("Auto-reply enabled but GEMINI_API_KEY not set")
        return

    # Skip customers with AI disabled (big/VIP customers handled manually)
    if await is_ai_disabled(phone):
        logger.debug("AI disabled for %s, skipping auto-reply", phone)
        return

    # Skip if customer is likely asleep (1am–6am local time)
    if _is_sleeping_hours(phone):
        hour = _get_customer_local_hour(phone)
        logger.info("Skipping auto-reply for %s — customer local time ~%02d:00 (sleeping hours)", phone, hour or 0)
        return

    # Skip non-text-like messages that don't need replies
    if msg_type in ("reaction", "sticker"):
        return

    # For media messages (image/video/audio/document), delay 10 minutes
    # to let Lucky reply first; if no human reply, send a brief acknowledgment
    if msg_type in ("image", "video", "audio", "document", "voice"):
        await asyncio.sleep(600)  # wait 10 minutes
        if await _has_recent_outbound(phone, window=600):
            logger.info("Human already replied to media from %s, skipping", phone)
            return
        # No human reply after 10 min — send brief acknowledgment
        msg_id = await send_text_message(phone, "let me check this, one moment 👍")
        if msg_id:
            _record_reply(phone)
            logger.info("Auto-acknowledged media from %s after 10min", phone)
        return

    # Acquire per-phone lock — only one reply task at a time per customer
    lock = _get_phone_lock(phone)
    if lock.locked():
        logger.debug("Auto-reply already in progress for %s, skipping", phone)
        return

    async with lock:
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

        # Dynamic delay: extend if Lucky was recently active on this phone
        delay = settings.auto_reply_delay
        human_last = _human_active_history.get(phone, 0)
        if (time.time() - human_last) < 14400:  # 4 hours
            delay = max(delay, 300)  # at least 5 minutes when human is active
            logger.info("Human recently active for %s, extended delay to %ds", phone, delay)

        # Delay to let Lucky or KnowBot respond first
        await asyncio.sleep(delay)

        # Re-check ALL conditions after delay
        if _check_human_takeover(phone):
            logger.info("Human takeover detected after delay for %s, skipping", phone)
            return

        if _check_cooldown(phone):
            logger.debug("Auto-reply cooldown active after delay for %s, skipping", phone)
            return

        # Check if anyone already replied during our delay (wider window)
        if await _has_recent_outbound(phone, window=delay + 30):
            logger.info("Outbound detected during delay for %s, skipping auto-reply", phone)
            return

        # Load conversation context
        messages = await get_messages_by_phone(
            phone, limit=settings.auto_reply_context_messages
        )
        conversation_text = _format_conversation(messages)

        # Build customer context from local DB
        ctx = await get_customer_context(phone)
        customer_context = _format_customer_context(ctx)
        logger.info(
            "Customer context for %s: stage=%s, msgs=%d, days=%d, known=%s",
            phone, ctx["relationship_stage"], ctx["total_messages"],
            ctx["first_seen_days"], ctx["is_known"],
        )

        # Build prompts
        knowledge = get_knowledge_text()
        reply_style = get_reply_style()
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            knowledge_base=knowledge, reply_style=reply_style
        )
        customer_name = display_name or phone
        user_prompt = USER_PROMPT_TEMPLATE.format(
            customer_name=customer_name,
            phone=phone,
            conversation_text=conversation_text,
            customer_context=customer_context,
        )

        # Call LLM (Gemini primary, Anthropic fallback)
        reply_text = await _call_gemini_reply(system_prompt, user_prompt)
        if not reply_text and settings.anthropic_api_key:
            logger.warning("Gemini failed for %s, falling back to Anthropic", phone)
            reply_text = await _call_anthropic_reply(system_prompt, user_prompt)
        if not reply_text:
            logger.warning("No reply generated for %s", phone)
            return

        # Check for duplicate content — don't send same message twice
        if _is_duplicate_reply(phone, reply_text):
            logger.info("Duplicate reply detected for %s, skipping: %s", phone, reply_text[:60])
            return

        # FINAL CHECK before sending — human may have replied during LLM call
        if _check_human_takeover(phone):
            logger.info("Human takeover detected before send for %s, discarding reply", phone)
            return
        if await _has_recent_outbound(phone, window=30):
            logger.info("Outbound detected before send for %s, discarding reply", phone)
            return

        # Send reply via WATI
        msg_id = await send_text_message(phone, reply_text)
        if msg_id:
            _record_reply(phone)
            _last_reply_text[phone] = reply_text
            logger.info("Auto-replied to %s: %s", phone, reply_text[:80])
        else:
            logger.error("Failed to send auto-reply to %s", phone)
