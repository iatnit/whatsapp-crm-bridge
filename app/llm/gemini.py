"""Shared Gemini API client for analysis and auto-reply."""

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Retryable HTTP status codes
_RETRYABLE_CODES = {429, 500, 502, 503}


async def call_gemini(
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    json_mode: bool = False,
    timeout: float = 60,
    max_retries: int = 2,
) -> str | None:
    """Call Google Gemini API and return the raw text response.

    Args:
        json_mode: If True, set responseMimeType to application/json.
        timeout: HTTP request timeout in seconds.
        max_retries: Number of retries on transient errors (429, 5xx).

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

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)

            if resp.status_code != 200:
                if resp.status_code in _RETRYABLE_CODES and attempt < max_retries:
                    delay = 2 ** (attempt + 1)  # 2s, 4s
                    logger.warning(
                        "Gemini API %d (attempt %d/%d), retrying in %ds...",
                        resp.status_code, attempt + 1, max_retries + 1, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
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

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            if attempt < max_retries:
                delay = 2 ** (attempt + 1)
                logger.warning(
                    "Gemini timeout/connect error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries + 1, delay, e,
                )
                await asyncio.sleep(delay)
                continue
            logger.error("Gemini API call failed after %d attempts: %s", max_retries + 1, e)
            return None
        except Exception as e:
            logger.error("Gemini API call failed: %s", e)
            return None

    logger.error("Gemini API exhausted %d retries, last error: %s", max_retries + 1, last_error)
    return None
