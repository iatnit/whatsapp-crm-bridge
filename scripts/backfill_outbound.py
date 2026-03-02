#!/usr/bin/env python3
"""One-time backfill: pull ALL historical outbound messages from WATI
and forward them to the local Obsidian receiver.

Run from project root:
    python scripts/backfill_outbound.py [--dry-run]
"""

import asyncio
import hashlib
import hmac
import json
import sys
from pathlib import Path

import httpx

# ── Config ────────────────────────────────────────────────────────────
WATI_V1_URL = "https://live-mt-server.wati.io/1096787"
WATI_TOKEN  = (
    Path(__file__).parent.parent / ".env"
)

RECEIVER_URL    = "http://127.0.0.1:8765/api/v1/message"
MAPPING_FILE    = Path(__file__).parent.parent / "local-receiver/data/phone_to_folder.json"
RECEIVER_SECRET = ""

PAGE_SIZE = 200   # messages per WATI API call

DRY_RUN = "--dry-run" in sys.argv

# ── Load secrets from .env ────────────────────────────────────────────
def _load_env() -> dict:
    env_path = Path(__file__).parent.parent / ".env"
    env: dict = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

# ── HMAC signing ──────────────────────────────────────────────────────
def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

# ── WATI API: paginate all messages for a phone ───────────────────────
async def fetch_all_messages(client: httpx.AsyncClient, phone: str, token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    all_items: list[dict] = []
    page = 1
    while True:
        url = f"{WATI_V1_URL}/api/v1/getMessages/{phone}?pageSize={PAGE_SIZE}&pageNumber={page}"
        try:
            resp = await client.get(url, headers=headers, timeout=15)
        except Exception as e:
            print(f"  ⚠ WATI request failed: {e}")
            break
        if resp.status_code != 200:
            break
        data = resp.json()
        items = data.get("messages", {}).get("items") or []
        all_items.extend(items)
        # Check if there are more pages
        total = data.get("messages", {}).get("totalCount") or 0
        if len(all_items) >= total or len(items) < PAGE_SIZE:
            break
        page += 1
    return all_items

# ── Forward one message to local receiver ────────────────────────────
async def forward(client: httpx.AsyncClient, secret: str, payload: dict) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode()
    sig  = _sign(secret, body)
    try:
        resp = await client.post(
            RECEIVER_URL,
            content=body,
            headers={"Content-Type": "application/json",
                     "X-Signature": f"hmac-sha256={sig}"},
            timeout=10,
        )
        return resp.status_code == 200 and resp.json().get("written")
    except Exception as e:
        print(f"    ⚠ forward failed: {e}")
        return False

# ── Main ──────────────────────────────────────────────────────────────
async def main():
    env = _load_env()
    token  = env.get("WATI_API_TOKEN", "")
    secret = env.get("OBSIDIAN_SYNC_SECRET", "")

    if not token:
        print("❌ WATI_API_TOKEN not found in .env"); sys.exit(1)
    if not secret:
        print("❌ OBSIDIAN_SYNC_SECRET not found in .env"); sys.exit(1)

    mapping: dict = {}
    if MAPPING_FILE.exists():
        mapping = json.loads(MAPPING_FILE.read_text())
    print(f"Loaded {len(mapping)} phone mappings")

    if DRY_RUN:
        print("🔍 DRY RUN — messages will be fetched but NOT forwarded\n")

    total_new = 0
    async with httpx.AsyncClient() as client:
        for i, (phone, folder) in enumerate(mapping.items(), 1):
            print(f"[{i}/{len(mapping)}] {folder} ({phone})")
            items = await fetch_all_messages(client, phone, token)
            outbound = [m for m in items if m.get("owner")]
            print(f"  {len(items)} messages total, {len(outbound)} outbound")

            new_count = 0
            for msg in outbound:
                msg_id   = msg.get("id", "")
                msg_type = msg.get("type") or "text"
                content  = msg.get("text") or ""
                ts       = int(msg.get("timestamp") or 0)

                if not content and msg_type != "text":
                    content = f"[{msg_type}]"

                payload = {
                    "wa_message_id": msg_id,
                    "phone":         phone,
                    "display_name":  folder,
                    "customer_name": folder,
                    "direction":     "outbound",
                    "msg_type":      msg_type,
                    "content":       content,
                    "timestamp":     ts,
                    "media_url":     "",
                }

                if DRY_RUN:
                    new_count += 1
                else:
                    written = await forward(client, secret, payload)
                    if written:
                        new_count += 1

            print(f"  ✅ {new_count} new outbound messages written")
            total_new += new_count

    print(f"\nDone. Total new outbound messages written: {total_new}")

if __name__ == "__main__":
    asyncio.run(main())
