"""Match WhatsApp contacts to CRM customers via fuzzy name matching."""

import json
import logging
from difflib import SequenceMatcher
from pathlib import Path

from app.config import settings
from app.store.conversations import update_customer_match

logger = logging.getLogger(__name__)

# customer DB: {"customer_id": "customer_name", ...}
_customer_db: dict[str, str] = {}


def load_customers() -> dict[str, str]:
    """Load the customer database from JSON."""
    global _customer_db
    path: Path = settings.customers_json
    if not path.exists():
        logger.warning("Customer JSON not found at %s", path)
        _customer_db = {}
        return _customer_db

    with open(path, encoding="utf-8") as f:
        _customer_db = json.load(f)
    logger.info("Loaded %d customers from %s", len(_customer_db), path)
    return _customer_db


def search_customer(name: str, threshold: float = 0.6) -> list[tuple[str, str, float]]:
    """Search for a customer by name.

    Returns list of (customer_id, customer_name, score) sorted by score desc.
    """
    if not _customer_db:
        load_customers()

    results: list[tuple[str, str, float]] = []

    for cid, cname in _customer_db.items():
        if not isinstance(cname, str) or not cname.strip():
            continue
        cname = cname.strip()

        # Exact match
        if name.lower() == cname.lower():
            return [(cid, cname, 1.0)]

        # Substring match
        if name.lower() in cname.lower() or cname.lower() in name.lower():
            results.append((cid, cname, 0.9))
            continue

        # Fuzzy match
        ratio = SequenceMatcher(None, name.lower(), cname.lower()).ratio()
        if ratio >= threshold:
            results.append((cid, cname, ratio))

    results.sort(key=lambda x: x[2], reverse=True)
    return results


async def match_conversation(phone: str, display_name: str) -> dict | None:
    """Try to match a WhatsApp display_name to a CRM customer.

    Returns {"customer_id", "customer_name", "score"} or None.
    """
    if not display_name:
        return None

    matches = search_customer(display_name)
    if not matches:
        return None

    best_id, best_name, score = matches[0]
    if score < 0.6:
        return None

    await update_customer_match(phone, best_id, best_name, "matched")
    logger.info(
        "Matched %s (%s) → %s [%s] (%.0f%%)",
        display_name, phone, best_name, best_id, score * 100,
    )
    return {"customer_id": best_id, "customer_name": best_name, "score": score}


async def match_all_unmatched() -> list[dict]:
    """Run matching for all unmatched conversations."""
    from app.store.conversations import get_unmatched_conversations

    load_customers()
    unmatched = await get_unmatched_conversations()
    results = []

    for conv in unmatched:
        result = await match_conversation(conv["phone"], conv["display_name"])
        if result:
            results.append({**result, "phone": conv["phone"]})

    logger.info("Matched %d / %d unmatched conversations", len(results), len(unmatched))
    return results
