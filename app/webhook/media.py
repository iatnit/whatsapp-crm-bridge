"""Download media files from WATI webhook payloads."""

import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Filename helpers ─────────────────────────────────────────────────

# Daily sequence counter: "safe_name-YYYYMMDD" → int
_daily_seq: dict[str, int] = {}


def sanitize_name(name: str) -> str:
    """Sanitize a customer name for use in filenames.

    - Strip whitespace, replace spaces with hyphens
    - Remove filesystem-unsafe characters
    - Collapse consecutive hyphens, limit to 50 chars
    - Fallback to 'unknown' if empty
    """
    if not name or not name.strip():
        return "unknown"
    name = name.strip()
    # Remove filesystem-unsafe characters
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '', name)
    # Replace whitespace with hyphens
    name = re.sub(r'\s+', '-', name)
    # Remove remaining non-word chars except hyphens/dots (keep unicode letters)
    name = re.sub(r'[^\w\-.]', '', name, flags=re.UNICODE)
    # Collapse consecutive hyphens
    name = re.sub(r'-{2,}', '-', name)
    name = name.strip('-')
    name = name[:50]
    return name or "unknown"


def _next_seq(key: str) -> int:
    """Return next sequence number for a name+date combination."""
    _daily_seq[key] = _daily_seq.get(key, 0) + 1
    return _daily_seq[key]


# ── Download ─────────────────────────────────────────────────────────

async def download_media(
    message_id: str,
    url: str,
    display_name: str = "",
    phone: str = "",
) -> str | None:
    """Download a media file from a URL provided by WATI.

    Args:
        message_id: WhatsApp message ID (fallback for filename).
        url: Direct download URL from WATI webhook payload.
        display_name: Customer WhatsApp display name.
        phone: Customer phone number.

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

        # Build human-readable filename: {name}-{YYYYMMDD}-{seq}.{ext}
        cst = timezone(timedelta(hours=8))
        date_str = datetime.now(cst).strftime("%Y%m%d")
        safe_name = sanitize_name(display_name or phone)
        seq_key = f"{safe_name}-{date_str}"
        seq = _next_seq(seq_key)
        filename = f"{safe_name}-{date_str}-{seq:02d}{ext}"

        dest: Path = settings.media_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Avoid collision with existing files
        while dest.exists():
            seq = _next_seq(seq_key)
            filename = f"{safe_name}-{date_str}-{seq:02d}{ext}"
            dest = settings.media_dir / filename

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
