#!/usr/bin/env python3
"""Deduplicate HubSpot contacts by normalized phone number.

Finds contacts where the same phone number was stored in different formats
(e.g. '+91 9876543210' vs '+919876543210' vs '919876543210') and merges them.

Strategy:
  - Normalize all phone numbers (strip spaces/hyphens, ensure + prefix)
  - Group contacts by normalized phone
  - For each duplicate group, pick the PRIMARY contact (most data wins)
  - Merge the rest into the primary via HubSpot merge API

Usage:
    python scripts/dedup_contacts.py --dry-run    # preview only
    python scripts/dedup_contacts.py              # merge for real
"""

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

import httpx
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"

# Stage priority for deciding which contact to keep as primary
_STAGE_ORDER = {
    "repeat_buyer": 7,
    "ordered": 6,
    "negotiating": 5,
    "qualified": 4,
    "contacted": 3,
    "new_lead": 2,
    "dormant": 1,
    "lost": 1,
    "": 0,
}

_LIST_PROPERTIES = [
    "phone", "firstname", "lastname", "country",
    "customer_stage", "product_interest", "customer_tags",
    "customer_type", "industry", "customer_tier",
    "whatsapp_number", "market_region", "comm_language",
    "first_contact_date", "last_contact_date",
]


def _headers():
    return {
        "Authorization": f"Bearer {settings.hubspot_access_token}",
        "Content-Type": "application/json",
    }


def normalize_phone(phone: str) -> str:
    """Strip spaces/hyphens/parens and ensure + prefix."""
    if not phone:
        return ""
    p = re.sub(r"[\s\-\(\)]", "", phone.strip())
    if not p.startswith("+"):
        p = "+" + p
    return p


def _score_contact(c: dict) -> int:
    """Higher score = keep as primary."""
    score = 0
    stage = c.get("customer_stage") or ""
    score += _STAGE_ORDER.get(stage, 0) * 100
    if c.get("customer_tier"):   score += 50
    if c.get("product_interest"): score += 20
    if c.get("customer_type"):    score += 10
    if c.get("country"):          score += 5
    if c.get("first_contact_date"): score += 2
    return score


async def list_all_contacts(client: httpx.AsyncClient) -> list[dict]:
    url = f"{BASE_URL}/crm/v3/objects/contacts"
    params = {"limit": 100, "properties": ",".join(_LIST_PROPERTIES)}
    all_contacts = []
    while True:
        resp = await client.get(url, params=params, headers=_headers())
        if resp.status_code != 200:
            logger.error("List contacts failed [%d]: %s", resp.status_code, resp.text[:200])
            break
        data = resp.json()
        for r in data.get("results", []):
            entry = {"id": r["id"]}
            entry.update(r.get("properties", {}))
            all_contacts.append(entry)
        paging = data.get("paging", {}).get("next")
        if paging:
            params["after"] = paging["after"]
        else:
            break
    logger.info("Loaded %d contacts from HubSpot", len(all_contacts))
    return all_contacts


async def merge_contacts(
    client: httpx.AsyncClient,
    primary_id: str,
    secondary_id: str,
    dry_run: bool,
) -> bool:
    """Merge secondary into primary via HubSpot merge API."""
    if dry_run:
        return True
    url = f"{BASE_URL}/crm/v3/objects/contacts/merge"
    payload = {"primaryObjectId": primary_id, "objectIdToMerge": secondary_id}
    resp = await client.post(url, json=payload, headers=_headers())
    if resp.status_code not in (200, 204):
        logger.error(
            "Merge failed (%s → %s) [%d]: %s",
            secondary_id, primary_id, resp.status_code, resp.text[:200],
        )
        return False
    return True


async def update_phone(
    client: httpx.AsyncClient,
    contact_id: str,
    normalized_phone: str,
    dry_run: bool,
) -> bool:
    """Update the contact's phone to the normalized form."""
    if dry_run:
        return True
    url = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"
    payload = {"properties": {"phone": normalized_phone, "whatsapp_number": normalized_phone}}
    resp = await client.patch(url, json=payload, headers=_headers())
    return resp.status_code == 200


async def main(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        contacts = await list_all_contacts(client)

        # Group by normalized phone
        from collections import defaultdict
        phone_groups: dict[str, list[dict]] = defaultdict(list)
        for c in contacts:
            phone = c.get("phone") or c.get("whatsapp_number") or ""
            norm = normalize_phone(phone)
            if norm and norm != "+":
                phone_groups[norm].append(c)

        dup_groups = {p: g for p, g in phone_groups.items() if len(g) > 1}
        total_extra = sum(len(g) - 1 for g in dup_groups.values())

        print(f"\n{'='*65}")
        print(f"  HUBSPOT CONTACT DEDUPLICATION REPORT")
        print(f"{'='*65}")
        print(f"  Total contacts:        {len(contacts)}")
        print(f"  Duplicate phone groups: {len(dup_groups)}")
        print(f"  Contacts to remove:    {total_extra}")
        if args.dry_run:
            print(f"  Mode:                  DRY RUN (no changes)")
        else:
            print(f"  Mode:                  LIVE (will merge contacts)")
        print(f"{'='*65}\n")

        stats = {"merged": 0, "errors": 0, "skipped": 0}

        for norm_phone, group in sorted(dup_groups.items()):
            # Sort: highest score first → that's our primary
            group_sorted = sorted(group, key=_score_contact, reverse=True)
            primary = group_sorted[0]
            secondaries = group_sorted[1:]

            primary_name = f"{primary.get('firstname','') or ''} {primary.get('lastname','') or ''}".strip()
            primary_raw_phone = primary.get("phone") or ""

            for sec in secondaries:
                sec_name = f"{sec.get('firstname','') or ''} {sec.get('lastname','') or ''}".strip()
                sec_raw_phone = sec.get("phone") or ""

                logger.info(
                    "Merging: [%s] %r (phone=%r) + [%s] %r (phone=%r) → primary [%s]",
                    sec["id"], sec_name, sec_raw_phone,
                    primary["id"], primary_name, primary_raw_phone,
                    primary["id"],
                )

                ok = await merge_contacts(client, primary["id"], sec["id"], args.dry_run)
                if ok:
                    stats["merged"] += 1
                    # Fix normalized phone on primary if needed
                    if primary_raw_phone != norm_phone and not args.dry_run:
                        await update_phone(client, primary["id"], norm_phone, args.dry_run)
                else:
                    stats["errors"] += 1

        print(f"\n  RESULT:")
        print(f"  Contacts merged: {stats['merged']}")
        print(f"  Errors:          {stats['errors']}")
        if args.dry_run:
            print(f"\n  [DRY RUN — re-run without --dry-run to apply]\n")
        else:
            print(f"\n  Done. HubSpot contacts deduplicated.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deduplicate HubSpot contacts by phone number.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    args = parser.parse_args()
    asyncio.run(main(args))
