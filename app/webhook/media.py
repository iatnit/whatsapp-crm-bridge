"""Download media files from WATI webhook payloads."""

import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Filename helpers ─────────────────────────────────────────────────


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


def _next_seq_from_files(media_dir: Path, safe_name: str, date_str: str) -> int:
    """Return next sequence by scanning existing files on disk.

    This keeps sequence stable across process restarts.
    """
    pattern = re.compile(
        rf"^{re.escape(safe_name)}-{re.escape(date_str)}-(\d+)\.[A-Za-z0-9]+$"
    )
    max_seq = 0
    if not media_dir.exists():
        return 1
    for file_path in media_dir.iterdir():
        if not file_path.is_file():
            continue
        m = pattern.match(file_path.name)
        if not m:
            continue
        try:
            max_seq = max(max_seq, int(m.group(1)))
        except ValueError:
            continue
    return max_seq + 1


# ── Download ─────────────────────────────────────────────────────────

async def download_media(
    message_id: str,
    url: str,
    customer_name: str = "",
    display_name: str = "",
    phone: str = "",
    timestamp: int = 0,
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

        # MIME type whitelist — reject unexpected content types to prevent
        # saving HTML error pages, JavaScript, or executable files as media
        _ALLOWED_MIME_PREFIXES = (
            "image/", "audio/", "video/",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument",
            "application/vnd.ms-",
            "application/msword",
            "application/zip",
            "application/octet-stream",
        )
        mime_base = content_type.split(";")[0].strip().lower()
        if mime_base and not any(mime_base.startswith(p) for p in _ALLOWED_MIME_PREFIXES):
            logger.warning(
                "Rejected media download for %s: unexpected MIME type '%s'",
                message_id, mime_base,
            )
            return None
        ext = _ext_from_mime(content_type) or _ext_from_url(url) or ".bin"

        # Build filename: {customer}-{YYYYMMDD}-{seq}.{ext}
        cst = timezone(timedelta(hours=8))
        if timestamp > 0:
            date_str = datetime.fromtimestamp(timestamp, tz=cst).strftime("%Y%m%d")
        else:
            date_str = datetime.now(cst).strftime("%Y%m%d")

        safe_name = sanitize_name(customer_name or display_name or phone)
        seq = _next_seq_from_files(settings.media_dir, safe_name, date_str)
        filename = f"{safe_name}-{date_str}-{seq:03d}{ext}"

        dest: Path = settings.media_dir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Avoid collision if another message is written at the same time.
        attempts = 0
        while dest.exists() and attempts < 50:
            seq += 1
            filename = f"{safe_name}-{date_str}-{seq:03d}{ext}"
            dest = settings.media_dir / filename
            attempts += 1
        if dest.exists():
            # Fallback: use message_id + timestamp for uniqueness
            ts = int(time.time())
            filename = f"{safe_name}-{date_str}-{ts}{ext}"
            dest = settings.media_dir / filename
            logger.warning("Media filename collision limit reached, using fallback: %s", filename)

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
