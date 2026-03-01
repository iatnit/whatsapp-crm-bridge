"""Shared Gemini API client for analysis and auto-reply."""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def call_gemini(
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    json_mode: bool = False,
    timeout: float = 60,
) -> str | None:
    """Call Google Gemini API and return the raw text response.

    Args:
        json_mode: If True, set responseMimeType to application/json.
        timeout: HTTP request timeout in seconds.

    Returns the text content or None on failure.
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={settings.gemini_api_key}"
    )
    generation_config: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if json_mode:
        generation_config["responseMimeType"] = "application/json"

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": generation_config,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            logger.error("Gemini API error %d: %s", resp.status_code, resp.text[:500])
            return None

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            logger.error("Gemini: no candidates in response")
            return None

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts or not parts[0].get("text"):
            logger.error("Gemini: empty content/parts in response")
            return None

        text = parts[0]["text"].strip()
        return text

    except Exception as e:
        logger.error("Gemini API call failed: %s", e)
        return None
