"""Local FastAPI receiver — writes WhatsApp messages to Obsidian CRM chat logs."""

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
    """Append a single message to the daily chat log file.

    Returns True if written, False if duplicate (idempotent).
    """
    crm_base = Path(settings.crm_base_path)
    folder_path = crm_base / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    # Determine date in CST
    if timestamp > 0:
        dt = datetime.fromtimestamp(timestamp, tz=CST)
    else:
        dt = datetime.now(tz=CST)

    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H:%M:%S")

    log_file = folder_path / f"chat-log-{date_str}.md"

    # Check idempotency: scan for existing wa_message_id in hidden comments
    if log_file.exists():
        existing = log_file.read_text(encoding="utf-8")
        if f"<!-- {wa_message_id} -->" in existing:
            return False  # already written
    else:
        existing = ""

    # Build the message line
    arrow = "<<<" if direction == "inbound" else ">>>"

    # Format content based on message type
    if msg_type in ("image", "video", "audio", "document", "voice"):
        if content and not content.startswith("["):
            display_content = f"[{msg_type}] {content}"
        else:
            display_content = content or f"[{msg_type}]"
    else:
        display_content = content

    line = f"[{time_str}] {arrow} {display_content}\n<!-- {wa_message_id} -->\n"

    # Create file with frontmatter if new
    if not existing:
        label = customer_name or display_name or folder_name
        header = (
            f"---\ntype: chat-log\ndate: {date_str}\n"
            f"customer: {label}\n---\n\n"
            f"# Chat Log - {label} - {date_str}\n\n"
        )
        log_file.write_text(header + line, encoding="utf-8")
    else:
        # Append to existing file
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)

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

    if not phone or not wa_message_id:
        return JSONResponse({"error": "missing phone or wa_message_id"}, status_code=400)

    # Resolve folder
    folder_name = _resolve_folder(phone, customer_name, display_name)

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
