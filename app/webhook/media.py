"""Download media files from WATI webhook payloads."""

import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def download_media(message_id: str, url: str) -> str | None:
    """Download a media file from a URL provided by WATI.

    Args:
        message_id: WhatsApp message ID (used as filename).
        url: Direct download URL from WATI webhook payload.

    Returns the local file path on success, None on failure.
    """
    if not url:
        return None

    headers = {"Authorization": f"Bearer {settings.wati_api_token}"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.RequestError as e:
            logger.error("Media download request failed for %s: %s", message_id, e)
            return None

        if resp.status_code != 200:
            logger.error(
                "Media download failed for %s: HTTP %d", message_id, resp.status_code
            )
            return None

        # Determine extension from Content-Type or URL
        content_type = resp.headers.get("content-type", "")
        ext = _ext_from_mime(content_type) or _ext_from_url(url) or ".bin"
        filename = f"{message_id}{ext}"

        dest: Path = settings.media_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)

        logger.info("Saved media %s → %s (%s)", message_id, dest, content_type)
        return str(dest)


def _ext_from_mime(mime: str) -> str:
    """Map MIME type to file extension."""
    ext_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "video/mp4": ".mp4",
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
    return ext_map.get(mime.split(";")[0].strip(), "")


def _ext_from_url(url: str) -> str:
    """Extract extension from URL path."""
    path = urlparse(url).path
    if "." in path.split("/")[-1]:
        return "." + path.rsplit(".", 1)[-1][:5]
    return ""
