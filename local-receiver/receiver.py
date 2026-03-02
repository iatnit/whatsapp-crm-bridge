"""Local FastAPI receiver — writes WhatsApp messages to Obsidian CRM chat logs."""

import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("obsidian-receiver")

app = FastAPI(title="Obsidian Chat Receiver", version="0.1.0")

CST = timezone(timedelta(hours=8))

# ── Media helpers ─────────────────────────────────────────────────


def _sanitize_name(name: str) -> str:
    """Sanitize customer name for use in filenames."""
    if not name or not name.strip():
        return "unknown"
    name = name.strip()
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '', name)
    name = re.sub(r'\s+', '-', name)
    name = re.sub(r'[^\w\-.]', '', name, flags=re.UNICODE)
    name = re.sub(r'-{2,}', '-', name)
    name = name.strip('-')[:50]
    return name or "unknown"


def _ext_from_mime(mime: str) -> str:
    ext_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "video/mp4": ".mp4",
        "application/pdf": ".pdf",
    }
    return ext_map.get(mime.split(";")[0].strip(), "")


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    if "." in path.split("/")[-1]:
        return "." + path.rsplit(".", 1)[-1][:5]
    return ""


def _next_seq(folder_path: Path, safe_name: str, date_str: str) -> int:
    """Scan folder for existing files and return the next sequence number."""
    pattern = re.compile(
        rf"^{re.escape(safe_name)}-{re.escape(date_str)}-(\d+)\.[A-Za-z0-9]+$"
    )
    max_seq = 0
    if not folder_path.exists():
        return 1
    for f in folder_path.iterdir():
        if not f.is_file():
            continue
        m = pattern.match(f.name)
        if m:
            try:
                max_seq = max(max_seq, int(m.group(1)))
            except ValueError:
                pass
    return max_seq + 1


async def _save_media(
    media_url: str,
    customer_name: str,
    display_name: str,
    phone: str,
    timestamp: int,
    folder_path: Path,
) -> str | None:
    """Download a media file from WATI and save it to the customer's CRM folder.

    Returns the saved filename on success, None on failure.
    Naming: {customer_name}-{YYYYMMDD}-{seq:03d}.{ext}
    """
    if not settings.wati_api_token:
        logger.warning("wati_api_token not configured — skipping media download")
        return None

    headers = {"Authorization": f"Bearer {settings.wati_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(media_url, headers=headers)
    except Exception as e:
        logger.warning("Media download request failed: %s", e)
        return None

    if resp.status_code != 200:
        logger.warning("Media download HTTP %d for url=%s", resp.status_code, media_url)
        return None

    content_type = resp.headers.get("content-type", "")
    ext = _ext_from_mime(content_type) or _ext_from_url(media_url) or ".bin"

    if timestamp > 0:
        date_str = datetime.fromtimestamp(timestamp, tz=CST).strftime("%Y%m%d")
    else:
        date_str = datetime.now(tz=CST).strftime("%Y%m%d")

    safe_name = _sanitize_name(customer_name or display_name or phone)
    seq = _next_seq(folder_path, safe_name, date_str)
    filename = f"{safe_name}-{date_str}-{seq:03d}{ext}"
    dest = folder_path / filename

    attempts = 0
    while dest.exists() and attempts < 50:
        seq += 1
        filename = f"{safe_name}-{date_str}-{seq:03d}{ext}"
        dest = folder_path / filename
        attempts += 1

    folder_path.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    logger.info("Saved media → %s", dest)
    return filename


# ── Audio transcription ───────────────────────────────────────────

_AUDIO_MIME: dict[str, str] = {
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}


async def _transcribe_audio(file_path: Path) -> str | None:
    """Transcribe an audio file to Chinese using Gemini API.

    Returns the transcribed/translated text, or None on failure.
    """
    if not settings.gemini_api_key:
        return None

    mime_type = _AUDIO_MIME.get(file_path.suffix.lower(), "audio/ogg")
    try:
        audio_b64 = base64.b64encode(file_path.read_bytes()).decode()
    except Exception as e:
        logger.warning("Failed to read audio file %s: %s", file_path, e)
        return None

    payload = {
        "contents": [{
            "parts": [
                {"text": "请转录这段音频的内容，用中文输出。如果原文是中文直接转录；如果是其他语言请翻译成中文。只返回转录或翻译后的文字，不要添加任何解释或说明。"},
                {"inline_data": {"mime_type": mime_type, "data": audio_b64}},
            ]
        }]
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={settings.gemini_api_key}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            logger.warning("Gemini transcription HTTP %d: %s", resp.status_code, resp.text[:200])
            return None
        text = (
            resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
        return text or None
    except Exception as e:
        logger.warning("Gemini transcription failed: %s", e)
        return None


# ── Phone-to-folder mapping ────────────────────────────────────────

_mapping: dict[str, str] | None = None


def _load_mapping() -> dict[str, str]:
    global _mapping
    if _mapping is not None:
        return _mapping
    mapping_path = Path(settings.mapping_file)
    if mapping_path.exists():
        try:
            _mapping = json.loads(mapping_path.read_text())
            logger.info("Loaded %d phone mappings", len(_mapping))
        except Exception:
            logger.warning("Failed to load mapping file, starting fresh")
            _mapping = {}
    else:
        _mapping = {}
    return _mapping


def _save_mapping() -> None:
    if _mapping is None:
        return
    mapping_path = Path(settings.mapping_file)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps(_mapping, ensure_ascii=False, indent=2))


def _resolve_folder(phone: str, customer_name: str, display_name: str) -> str:
    """Resolve which CRM folder to use for this phone number.

    Priority: cached mapping > customer_name > display_name > Unknown-{phone}
    Also scans existing CRM folders for a match if not cached.
    """
    mapping = _load_mapping()

    # Check cached mapping
    if phone in mapping:
        return mapping[phone]

    # Determine name to use
    name = customer_name.strip() if customer_name else ""
    if not name:
        name = display_name.strip() if display_name else ""
    if not name:
        name = f"Unknown-{phone}"

    crm_base = Path(settings.crm_base_path)

    # Try exact match with existing folders
    if (crm_base / name).exists():
        mapping[phone] = name
        _save_mapping()
        return name

    # Try case-insensitive match against existing folders
    if crm_base.exists():
        name_lower = name.lower()
        for folder in crm_base.iterdir():
            if folder.is_dir() and folder.name.lower() == name_lower:
                mapping[phone] = folder.name
                _save_mapping()
                return folder.name

    # No match found — use the name as-is (folder will be created)
    mapping[phone] = name
    _save_mapping()
    return name


# ── HMAC verification ──────────────────────────────────────────────

def _verify_signature(body: bytes, signature_header: str) -> bool:
    """Verify HMAC-SHA256 signature from X-Signature header."""
    if not settings.sync_secret:
        return True  # no secret configured = skip verification

    prefix = "hmac-sha256="
    if not signature_header.startswith(prefix):
        return False

    expected = hmac.new(
        settings.sync_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    received = signature_header[len(prefix):]
    return hmac.compare_digest(expected, received)


# ── Seen-IDs helpers ──────────────────────────────────────────────

def _is_seen(folder_path: Path, wa_message_id: str) -> bool:
    """Check if a message ID has already been written."""
    ids_file = folder_path / "seen_ids.txt"
    if ids_file.exists():
        return wa_message_id in ids_file.read_text(encoding="utf-8").splitlines()
    # Backward compat: check embedded comments in chat-log.md
    log_file = folder_path / "chat-log.md"
    if log_file.exists():
        return f"<!-- {wa_message_id} -->" in log_file.read_text(encoding="utf-8")
    return False


def _mark_seen(folder_path: Path, wa_message_id: str) -> None:
    """Append message ID to seen_ids.txt."""
    ids_file = folder_path / "seen_ids.txt"
    with ids_file.open("a", encoding="utf-8") as f:
        f.write(wa_message_id + "\n")


# ── Chat log writing ──────────────────────────────────────────────

def _write_message(
    folder_name: str,
    wa_message_id: str,
    direction: str,
    msg_type: str,
    content: str,
    timestamp: int,
    customer_name: str,
    display_name: str,
) -> bool:
    """Append a single message to the customer's single chat-log.md file.

    Returns True if written, False if duplicate (idempotent).
    """
    crm_base = Path(settings.crm_base_path)
    folder_path = crm_base / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    if timestamp > 0:
        dt = datetime.fromtimestamp(timestamp, tz=CST)
    else:
        dt = datetime.now(tz=CST)

    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H:%M:%S")

    log_file = folder_path / "chat-log.md"

    # Check idempotency
    if _is_seen(folder_path, wa_message_id):
        return False

    # Sender name
    sender = "Lucky" if direction == "outbound" else (customer_name or display_name or folder_name)

    display_content = content or f"[{msg_type}]"

    line = f"[{time_str}] {sender}: {display_content}\n"

    existing = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    if not existing:
        # New file
        label = customer_name or display_name or folder_name
        header = f"---\ntype: chat-log\ncustomer: {label}\n---\n\n# Chat Log - {label}\n\n"
        log_file.write_text(header + f"## {date_str}\n\n" + line, encoding="utf-8")
    else:
        # Append — add date header if this is a new day
        date_header = f"## {date_str}"
        with log_file.open("a", encoding="utf-8") as f:
            if date_header not in existing:
                f.write(f"\n{date_header}\n\n")
            f.write(line)

    _mark_seen(folder_path, wa_message_id)
    return True


# ── CRM summary writing ───────────────────────────────────────────

def _write_summary(
    folder_name: str,
    date: str,
    customer_name: str,
    phone: str,
    location: str,
    summary: str,
    demand_summary: str,
    followup_title: str,
    followup_detail: str,
    recommended_codes: list[str],
    next_actions: list[str],
    tags: list[str],
) -> str:
    """Write (overwrite) a CRM summary file into the customer's CRM folder.

    Returns the file path relative to crm_base_path.
    """
    crm_base = Path(settings.crm_base_path)
    folder_path = crm_base / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    # Build slug from customer name: lowercase, spaces/special → hyphens
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", customer_name).strip("-").lower() or "unknown"
    filename = f"crm-{slug}-{date}.md"
    file_path = folder_path / filename

    # Build recommended products section
    products_lines = ""
    if recommended_codes:
        products_lines = "\n".join(f"- {code}" for code in recommended_codes)

    # Build pending actions section
    actions_lines = ""
    if next_actions:
        items = []
        for action in next_actions:
            if action.lower().startswith("(waiting)") or action.lower().startswith("waiting"):
                items.append(f"- [ ] {action}")
            else:
                items.append(f"- [ ] {action}")
        actions_lines = "\n".join(items)

    # Build tags
    tags_str = ", ".join(tags) if tags else ""

    content = f"""---
type: crm
customer: {customer_name}
created: {date}
source: auto-pipeline
tags: [{tags_str}]
---

# CRM - {customer_name}

## Basic Info
| Field | Value |
|-------|-------|
| **WhatsApp** | {phone} |
| **Location** | {location or 'N/A'} |

## Summary
{summary or 'N/A'}

## Demand
{demand_summary or 'N/A'}

## Recommended Products
{products_lines or 'N/A'}

## Communication Log
### {date}
**{followup_title or 'WhatsApp沟通'}**
{followup_detail or summary or 'N/A'}

## Pending Actions
{actions_lines or '- [ ] No pending actions'}
"""

    file_path.write_text(content, encoding="utf-8")
    return str(file_path.relative_to(crm_base))


# ── Endpoints ──────────────────────────────────────────────────────

@app.post("/api/v1/message")
async def receive_message(request: Request):
    """Receive a forwarded WhatsApp message and write to Obsidian chat log."""
    body = await request.body()

    # Verify HMAC signature
    signature = request.headers.get("X-Signature", "")
    if not _verify_signature(body, signature):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        data = json.loads(body)
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    wa_message_id = data.get("wa_message_id", "")
    phone = data.get("phone", "")
    display_name = data.get("display_name", "")
    customer_name = data.get("customer_name", "")
    direction = data.get("direction", "inbound")
    msg_type = data.get("msg_type", "text")
    content = data.get("content", "")
    timestamp = int(data.get("timestamp", 0))
    media_url = data.get("media_url", "")

    if not phone or not wa_message_id:
        return JSONResponse({"error": "missing phone or wa_message_id"}, status_code=400)

    # Resolve folder
    folder_name = _resolve_folder(phone, customer_name, display_name)
    folder_path = Path(settings.crm_base_path) / folder_name

    # Download media to customer's CRM folder (image/video/document/etc.)
    if media_url:
        saved_filename = await _save_media(
            media_url=media_url,
            customer_name=customer_name,
            display_name=display_name,
            phone=phone,
            timestamp=timestamp,
            folder_path=folder_path,
        )
        if saved_filename:
            _type_label = {"image": "图片", "video": "视频", "audio": "音频", "voice": "音频", "document": "文件"}
            label = _type_label.get(msg_type, "文件")
            caption = content if content and not content.startswith("[") else ""

            # Transcribe audio/voice messages to Chinese
            if msg_type in ("audio", "voice"):
                transcript = await _transcribe_audio(folder_path / saved_filename)
                if transcript:
                    content = f"【{label}：{saved_filename}】「{transcript}」"
                else:
                    content = f"【{label}：{saved_filename}】"
            else:
                content = f"{caption} 【{label}：{saved_filename}】".strip() if caption else f"【{label}：{saved_filename}】"

    # Write to chat log
    written = _write_message(
        folder_name=folder_name,
        wa_message_id=wa_message_id,
        direction=direction,
        msg_type=msg_type,
        content=content,
        timestamp=timestamp,
        customer_name=customer_name,
        display_name=display_name,
    )

    return {
        "status": "ok",
        "written": written,
        "folder": folder_name,
    }


@app.post("/api/v1/summary")
async def receive_summary(request: Request):
    """Receive a CRM summary from daily pipeline and write to Obsidian."""
    body = await request.body()

    # Verify HMAC signature
    signature = request.headers.get("X-Signature", "")
    if not _verify_signature(body, signature):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        data = json.loads(body)
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    customer_name = data.get("customer_name", "")
    phone = data.get("phone", "")
    display_name = data.get("display_name", "")

    if not customer_name and not phone:
        return JSONResponse({"error": "missing customer_name or phone"}, status_code=400)

    # Use today's date in CST if not provided
    date = data.get("date", "") or datetime.now(tz=CST).strftime("%Y-%m-%d")

    # Resolve folder
    folder_name = _resolve_folder(phone, customer_name, display_name)

    # Write summary file (overwrite = idempotent)
    rel_path = _write_summary(
        folder_name=folder_name,
        date=date,
        customer_name=customer_name or display_name or folder_name,
        phone=phone,
        location=data.get("location", ""),
        summary=data.get("summary", ""),
        demand_summary=data.get("demand_summary", ""),
        followup_title=data.get("followup_title", ""),
        followup_detail=data.get("followup_detail", ""),
        recommended_codes=data.get("recommended_codes", []),
        next_actions=data.get("next_actions", []),
        tags=data.get("tags", []),
    )

    logger.info("Summary written: %s/%s", folder_name, rel_path)
    return {"status": "ok", "file": rel_path, "folder": folder_name}


@app.get("/health")
async def health():
    crm_path = Path(settings.crm_base_path)
    return {
        "status": "ok",
        "crm_path_exists": crm_path.exists(),
        "mappings_loaded": len(_load_mapping()),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
