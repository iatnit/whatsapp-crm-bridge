"""Call LLM API (Anthropic or Gemini) to analyze a WhatsApp conversation."""

import json
import logging

import httpx

from app.config import settings
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
    # Strip ```json ... ``` fences
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
    """Call Google Gemini API."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={settings.gemini_api_key}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            logger.error("Gemini API error %d: %s", resp.status_code, resp.text[:500])
            return None

        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        logger.info("Gemini analysis complete (%d chars)", len(text))
        return _parse_llm_text(text)

    except Exception as e:
        logger.error("Gemini API call failed: %s", e)
        return None


async def _call_anthropic(user_prompt: str) -> dict | None:
    """Call Anthropic Claude API (async)."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

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

    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        return None
