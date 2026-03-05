"""Match WhatsApp contacts to CRM customers via fuzzy name matching."""

import json
import logging
from difflib import SequenceMatcher
from pathlib import Path

from app.config import settings
from app.store.conversations import update_customer_match

logger = logging.getLogger(__name__)

# Minimum customers expected — if Feishu returns fewer, skip overwrite (safety)
_MIN_CUSTOMERS_THRESHOLD = 10

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

    try:
        with open(path, encoding="utf-8") as f:
            _customer_db = json.load(f)
        logger.info("Loaded %d customers from %s", len(_customer_db), path)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load customer JSON from %s: %s", path, e)
        _customer_db = {}
    return _customer_db


def search_customer(name: str, threshold: float = 0.75) -> list[tuple[str, str, float]]:
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

        # Substring match — require both strings to be at least 3 chars
        # to avoid false positives like "AK" matching "Dipak"
        if len(name) >= 3 and len(cname) >= 3:
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
    if score < 0.75:
        return None

    await update_customer_match(phone, best_id, best_name, "matched")
    logger.info(
        "Matched %s (%s) → %s [%s] (%.0f%%)",
        display_name, phone, best_name, best_id, score * 100,
    )
    return {"customer_id": best_id, "customer_name": best_name, "score": score}


async def sync_from_feishu() -> int:
    """Fetch all customers from Feishu and update crm_customers.json + in-memory DB.

    Runs at startup and every 4 hours so new Feishu customers are immediately
    available for fuzzy matching without manual file updates.

    Returns the number of customers now in the DB.
    """
    global _customer_db

    try:
        from app.writers.feishu_writer import list_customers_with_feishu_id
        customers = await list_customers_with_feishu_id()
    except Exception as e:
        logger.error("Failed to fetch customers from Feishu: %s", e)
        return len(_customer_db)

    if len(customers) < _MIN_CUSTOMERS_THRESHOLD:
        logger.warning(
            "Feishu returned only %d customers (< %d threshold) — skipping crm_customers.json update",
            len(customers), _MIN_CUSTOMERS_THRESHOLD,
        )
        return len(_customer_db)

    new_db = {c["feishu_id"]: c["name"] for c in customers if c.get("feishu_id") and c.get("name")}

    path: Path = settings.customers_json
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(new_db, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.error("Failed to write crm_customers.json: %s", e)
        # Still update in-memory even if disk write fails
        _customer_db = new_db
        return len(_customer_db)

    prev_count = len(_customer_db)
    _customer_db = new_db
    added = len(new_db) - prev_count
    logger.info(
        "Feishu customer sync: %d customers loaded (+%d new), saved to %s",
        len(new_db), max(added, 0), path,
    )
    return len(_customer_db)


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
