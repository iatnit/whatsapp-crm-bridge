"""Download media files (images, documents, audio) from the WhatsApp Cloud API."""

import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MEDIA_URL = "https://graph.facebook.com/v21.0"


async def download_media(media_id: str) -> str | None:
    """Download a media file by its WhatsApp media ID.

    Returns the local file path on success, None on failure.
    """
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1 – get the download URL
        resp = await client.get(f"{MEDIA_URL}/{media_id}", headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to get media URL for %s: %s", media_id, resp.text)
            return None

        data = resp.json()
        download_url = data.get("url")
        mime_type = data.get("mime_type", "application/octet-stream")
        if not download_url:
            logger.error("No URL in media response for %s", media_id)
            return None

        # Step 2 – download the file
        resp = await client.get(download_url, headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to download media %s: %s", media_id, resp.status_code)
            return None

        # Determine extension from MIME type
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
        ext = ext_map.get(mime_type, ".bin")
        filename = f"{media_id}{ext}"

        dest: Path = settings.media_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)

        logger.info("Saved media %s → %s (%s)", media_id, dest, mime_type)
        return str(dest)
