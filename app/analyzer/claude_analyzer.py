"""Call LLM API (Anthropic or Gemini) to analyze a WhatsApp conversation."""

import json
import logging

from app.config import settings
from app.llm.gemini import call_gemini
from app.analyzer.prompts import (
    SYSTEM_PROMPT,
    ANALYSIS_PROMPT_TEMPLATE,
    build_conversation_text,
)

logger = logging.getLogger(__name__)


async def analyze_conversation(
    messages: list[dict],
    customer_name: str,
    phone: str,
) -> dict | None:
    """Analyze a list of messages for one customer and return structured JSON."""
    if not messages:
        return None

    conversation_text = build_conversation_text(messages)
    user_prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        customer_name=customer_name,
        phone=phone,
        conversation_text=conversation_text,
    )

    if settings.llm_provider == "gemini":
        result = await _call_gemini(user_prompt)
        if result is None and settings.anthropic_api_key:
            logger.warning("Gemini failed, falling back to Anthropic")
            result = await _call_anthropic(user_prompt)
        return result
    else:
        return await _call_anthropic(user_prompt)


def _parse_llm_text(text: str) -> dict | None:
    """Strip markdown fences and parse JSON from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response as JSON: %s\nRaw: %s", e, text[:500])
        return None


async def _call_gemini(user_prompt: str) -> dict | None:
    """Call Gemini with JSON mode for structured analysis."""
    text = await call_gemini(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        json_mode=True,
        max_tokens=2048,
        timeout=60,
    )
    if not text:
        return None
    logger.info("Gemini analysis complete (%d chars)", len(text))
    return _parse_llm_text(text)


async def _call_anthropic(user_prompt: str) -> dict | None:
    """Call Anthropic Claude API (async)."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=60)

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = response.content[0].text
        logger.info("Claude analysis complete (%d chars)", len(text))
        return _parse_llm_text(text)

    except Exception as e:
        logger.error("Claude API error: %s", e)
        return None
