"""HubSpot CRM HTTP API client.

Directly calls HubSpot CRM v3 API via httpx — no SDK dependency.
Uses Private App Bearer token (static, no refresh needed).
Handles contact search/upsert, note creation, and deal creation.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings
from app.utils.phone import normalize_phone

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"

# ── Shared HTTP client (TCP connection reuse) ─────────────────────────
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    """Get or create shared httpx client for TCP connection reuse."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=15)
    return _http


async def close_http_client():
    """Close the shared httpx client on shutdown."""
    global _http
    if _http and not _http.is_closed:
        await _http.aclose()
        _http = None


# ── Auth ─────────────────────────────────────────────────────────────

def _headers() -> dict:
    """Static Bearer token from HubSpot Private App."""
    return {
        "Authorization": f"Bearer {settings.hubspot_access_token}",
        "Content-Type": "application/json",
    }


# ── Contact cache & lock ─────────────────────────────────────────────

# Stage ordering for regression prevention (higher = more advanced)
_STAGE_ORDER = {
    "new_lead": 1,
    "contacted": 2,
    "qualified": 3,
    "negotiating": 4,
    "ordered": 5,
    "repeat_buyer": 6,
    "dormant": 0,   # can be overridden by any active stage
    "lost": 0,      # can be overridden by any active stage
}

_contact_lock = asyncio.Lock()
_contact_cache: dict[str, str] = {}        # normalized phone → contact_id
_stage_cache: dict[str, str] = {}          # contact_id → current customer_stage

# Note dedup cache: "phone|YYYY-MM-DD" → note_id
_note_cache: dict[str, str] = {}


def clear_contact_cache():
    """Clear in-memory caches. Call at pipeline start."""
    _contact_cache.clear()
    _stage_cache.clear()
    _note_cache.clear()
    _deal_cache.clear()


# ── Contact operations ───────────────────────────────────────────────

async def search_contact_by_phone(phone: str) -> str | None:
    """Search HubSpot for a contact by phone number.

    Returns contact ID if found, None otherwise.
    """
    normalized = normalize_phone(phone)
    url = f"{BASE_URL}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "phone",
                        "operator": "EQ",
                        "value": normalized,
                    }
                ]
            }
        ],
        "properties": ["phone", "firstname", "lastname", "country", "customer_stage"],
        "limit": 1,
    }

    client = _get_http()
    resp = await client.post(url, json=payload, headers=_headers())

    if resp.status_code != 200:
        logger.error("HubSpot contact search failed [%d]: %s", resp.status_code, resp.text[:200])
        return None

    data = resp.json()
    results = data.get("results", [])
    if results:
        contact_id = results[0].get("id")
        # Cache current stage for regression prevention
        props = results[0].get("properties", {})
        current_stage = props.get("customer_stage", "")
        if contact_id and current_stage:
            _stage_cache[contact_id] = current_stage
        logger.debug("HubSpot found contact %s for phone %s (stage=%s)", contact_id, normalized, current_stage)
        return contact_id
    return None


async def create_contact(
    phone: str, name: str = "", country: str = "",
    extra: dict | None = None,
) -> str | None:
    """Create a new HubSpot contact.

    Args:
        extra: Additional LOCA custom properties to set.

    Returns the new contact ID or None.
    """
    normalized = normalize_phone(phone)
    # Split name into first/last
    parts = name.strip().split(maxsplit=1) if name else []
    firstname = parts[0] if parts else normalized
    lastname = parts[1] if len(parts) > 1 else ""

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    properties = {
        "phone": normalized,
        "firstname": firstname,
        "lastname": lastname,
        "whatsapp_number": normalized,
        "lead_source_channel": "whatsapp",
        "first_contact_date": today,
        "last_contact_date": today,
    }
    if country:
        properties["country"] = country

    # Merge LOCA custom properties (may override first_contact_date if provided)
    if extra:
        properties.update(extra)

    url = f"{BASE_URL}/crm/v3/objects/contacts"

    client = _get_http()
    resp = await client.post(url, json={"properties": properties}, headers=_headers())

    if resp.status_code not in (200, 201):
        logger.error("HubSpot create contact failed [%d]: %s", resp.status_code, resp.text[:200])
        return None

    contact_id = resp.json().get("id")
    logger.info("HubSpot created contact %s for %s (%s)", contact_id, name, normalized)
    return contact_id


async def update_contact(
    contact_id: str, name: str = "", country: str = "",
    extra: dict | None = None,
) -> bool:
    """Update an existing HubSpot contact.

    Args:
        extra: Additional LOCA custom properties to set.

    Returns True on success.
    """
    properties: dict = {}
    if name:
        parts = name.strip().split(maxsplit=1)
        properties["firstname"] = parts[0]
        if len(parts) > 1:
            properties["lastname"] = parts[1]
    if country:
        properties["country"] = country

    # Merge LOCA custom properties
    if extra:
        properties.update(extra)

    if not properties:
        return True  # nothing to update

    url = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"

    client = _get_http()
    resp = await client.patch(url, json={"properties": properties}, headers=_headers())

    if resp.status_code != 200:
        logger.error("HubSpot update contact %s failed [%d]: %s", contact_id, resp.status_code, resp.text[:200])
        return False

    logger.debug("HubSpot updated contact %s", contact_id)
    return True


async def ensure_contact(
    phone: str, name: str = "", country: str = "",
    extra: dict | None = None,
) -> str | None:
    """Search for a contact by phone; create if not found.

    Uses lock + cache to prevent concurrent duplicate creation.
    Returns contact ID or None.
    """
    normalized = normalize_phone(phone)

    # Fast path: cache hit
    if normalized in _contact_cache:
        # Still update properties if extra data is provided
        if extra:
            await update_contact(_contact_cache[normalized], extra=extra)
        return _contact_cache[normalized]

    async with _contact_lock:
        # Double-check after lock
        if normalized in _contact_cache:
            if extra:
                await update_contact(_contact_cache[normalized], extra=extra)
            return _contact_cache[normalized]

        contact_id = await search_contact_by_phone(phone)
        if contact_id:
            # Update name/country/extra if provided
            # Remove first_contact_date from updates to preserve original value
            update_extra = dict(extra) if extra else {}
            update_extra.pop("first_contact_date", None)

            # Prevent customer_stage regression
            new_stage = update_extra.get("customer_stage", "")
            if new_stage and contact_id in _stage_cache:
                current_stage = _stage_cache[contact_id]
                if _STAGE_ORDER.get(new_stage, 0) <= _STAGE_ORDER.get(current_stage, 0):
                    update_extra.pop("customer_stage", None)
                    logger.debug("Stage regression prevented: %s → %s for %s", current_stage, new_stage, contact_id)

            if name or country or update_extra:
                await update_contact(contact_id, name=name, country=country, extra=update_extra or None)
            _contact_cache[normalized] = contact_id
            return contact_id

        logger.info("HubSpot contact not found for %s, creating", normalized)
        contact_id = await create_contact(phone, name=name, country=country, extra=extra)
        if contact_id:
            _contact_cache[normalized] = contact_id
        return contact_id


# ── Analysis → HubSpot property mapper ──────────────────────────────

# Map from LOCA product code prefix to HubSpot product_interest value
_PRODUCT_CODE_MAP = {
    "DR": "DR", "DS": "DS", "DT": "DT", "DF": "DF",
    "PVC": "PVC", "MA": "MA", "SP": "SP",
}

# Map from analysis location keywords to market region
_REGION_KEYWORDS = {
    "india": "south_asia", "pakistan": "south_asia", "bangladesh": "south_asia",
    "sri lanka": "south_asia", "nepal": "south_asia",
    "vietnam": "southeast_asia", "thailand": "southeast_asia",
    "indonesia": "southeast_asia", "philippines": "southeast_asia",
    "malaysia": "southeast_asia", "cambodia": "southeast_asia",
    "myanmar": "southeast_asia",
    "dubai": "middle_east", "uae": "middle_east", "saudi": "middle_east",
    "turkey": "middle_east", "iran": "middle_east", "qatar": "middle_east",
    "egypt": "africa", "nigeria": "africa", "kenya": "africa",
    "south africa": "africa", "morocco": "africa", "ethiopia": "africa",
    "brazil": "latin_america", "mexico": "latin_america",
    "colombia": "latin_america", "argentina": "latin_america",
    "italy": "europe", "spain": "europe", "france": "europe",
    "germany": "europe", "uk": "europe", "portugal": "europe",
    "usa": "north_america", "canada": "north_america",
}

# Map analysis language to HubSpot comm_language value
_LANG_MAP = {
    "english": "english", "英语": "english", "英文": "english",
    "hindi": "hindi", "印地语": "hindi",
    "arabic": "arabic", "阿拉伯语": "arabic",
    "spanish": "spanish", "西班牙语": "spanish",
    "french": "french", "法语": "french",
}


def build_hubspot_properties(
    analysis: dict, phone: str, total_messages: int = 0,
) -> dict:
    """Extract HubSpot custom properties from the LLM analysis result.

    Args:
        total_messages: Total message count for this conversation (for stage calc).

    Returns a dict of HubSpot property name → value ready for API write.
    Only includes properties that have meaningful values.
    """
    props: dict = {}
    customer_info = analysis.get("customer_info", {})
    crm_fields = analysis.get("crm_fields", {})
    tags = analysis.get("tags", [])

    # ── Product interest (from recommended_codes) ──
    codes = analysis.get("recommended_codes", [])
    product_prefixes = set()
    for code in codes:
        prefix = code.upper().split("-")[0].split("_")[0]
        if prefix in _PRODUCT_CODE_MAP:
            product_prefixes.add(_PRODUCT_CODE_MAP[prefix])
    if product_prefixes:
        props["product_interest"] = ";".join(sorted(product_prefixes))

    # ── Location → city + market_region ──
    location = customer_info.get("location", "")
    if location:
        props["customer_city"] = location
        loc_lower = location.lower()
        for keyword, region in _REGION_KEYWORDS.items():
            if keyword in loc_lower:
                props["market_region"] = region
                break

    # ── Communication language ──
    language = customer_info.get("language", "")
    if language:
        lang_lower = language.lower().strip()
        for key, value in _LANG_MAP.items():
            if key in lang_lower:
                props["comm_language"] = value
                break

    # ── Customer stage (smart auto-progression) ──
    # Determine stage from multiple signals (highest applicable wins)
    stage = "new_lead"

    # Has back-and-forth conversation → contacted
    if total_messages > 3 or not analysis.get("is_new_customer"):
        stage = "contacted"

    # Has product interest or recommended codes → qualified
    if codes or crm_fields.get("industry"):
        stage = "qualified"

    # Discussing MOQ/price → negotiating
    moq = crm_fields.get("moq_qualified")
    ps = crm_fields.get("price_sensitivity", "unknown")
    if moq is not None or (ps and ps != "unknown"):
        stage = "negotiating"

    props["customer_stage"] = stage

    # ── Customer tags (from priority tags) ──
    tag_values = []
    for tag in tags:
        if "priority/high" in tag:
            tag_values.append("hot_lead")
    if analysis.get("is_new_customer"):
        tag_values.append("first_timer")
    if tag_values:
        props["customer_tags"] = ";".join(tag_values)

    # ── crm_fields from expanded LLM output ──
    if crm_fields:
        # Customer type
        ct = crm_fields.get("customer_type", "unknown")
        if ct and ct != "unknown":
            props["customer_type"] = ct

        # Industry
        industries = crm_fields.get("industry", [])
        if industries:
            props["industry"] = ";".join(industries)

        # Competitor
        competitors = crm_fields.get("competitor_mentioned", [])
        valid_competitors = []
        for c in competitors:
            c_lower = c.lower()
            if "amy" in c_lower:
                valid_competitors.append("amy")
            elif "coco" in c_lower:
                valid_competitors.append("coco")
            elif "yang" in c_lower:
                valid_competitors.append("yang")
            elif "preciosa" in c_lower:
                valid_competitors.append("preciosa")
            else:
                valid_competitors.append("other")
        if valid_competitors:
            props["competitor_using"] = ";".join(set(valid_competitors))

        # MOQ qualified
        moq = crm_fields.get("moq_qualified")
        if moq is True:
            props["moq_qualified"] = "true"
            props["customer_tier"] = "A"
        elif moq is False:
            props["moq_qualified"] = "false"
            props["customer_tier"] = "C"

        # Price sensitivity
        ps = crm_fields.get("price_sensitivity", "unknown")
        if ps and ps != "unknown":
            props["price_sensitivity"] = ps

    # ── WhatsApp number & lead source (always set) ──
    if phone:
        normalized = normalize_phone(phone)
        props["whatsapp_number"] = normalized
    props["lead_source_channel"] = "whatsapp"

    # ── Contact dates (last_contact_date always updated) ──
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    props["last_contact_date"] = today

    return props


# ── Note operations ──────────────────────────────────────────────────

async def create_note(
    contact_id: str,
    title: str,
    body: str,
    summary: str = "",
) -> str | None:
    """Create a Note in HubSpot and associate it with a Contact.

    Returns the note ID or None.
    """
    # Build note body
    note_body = f"**{title}**\n\n{body}"
    if summary:
        note_body = f"**摘要**: {summary}\n\n{note_body}"

    # Step 1: Create the note (engagement)
    url = f"{BASE_URL}/crm/v3/objects/notes"
    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note → contact
                    }
                ],
            }
        ],
    }

    client = _get_http()
    resp = await client.post(url, json=payload, headers=_headers())

    if resp.status_code not in (200, 201):
        logger.error("HubSpot create note failed [%d]: %s", resp.status_code, resp.text[:200])
        return None

    note_id = resp.json().get("id")
    logger.info("HubSpot created note %s for contact %s", note_id, contact_id)
    return note_id


async def ensure_note(
    contact_id: str,
    phone: str,
    title: str,
    detail: str,
    summary: str = "",
) -> str | None:
    """Create a note with per-day dedup (one note per phone per day).

    Uses in-memory cache to avoid duplicates within the same pipeline run.
    """
    cst = timezone(timedelta(hours=8))
    today_str = datetime.now(cst).strftime("%Y-%m-%d")
    cache_key = f"{normalize_phone(phone)}|{today_str}"

    if cache_key in _note_cache:
        logger.info("HubSpot note cache hit for %s, skipping", cache_key)
        return _note_cache[cache_key]

    note_id = await create_note(contact_id, title=title, body=detail, summary=summary)
    if note_id:
        _note_cache[cache_key] = note_id
    return note_id


# ── Deal operations ──────────────────────────────────────────────────

_deal_cache: dict[str, str] = {}  # "contact_id|deal_name" → deal_id


async def ensure_deal(
    contact_id: str,
    deal_name: str,
    amount: float = 0,
    stage: str = "closedwon",
    close_date: str = "",
) -> str | None:
    """Create a deal with dedup: skip if same contact+name already created."""
    cache_key = f"{contact_id}|{deal_name}"
    if cache_key in _deal_cache:
        logger.info("HubSpot deal cache hit: %s, skipping", deal_name)
        return _deal_cache[cache_key]
    deal_id = await create_deal(contact_id, deal_name, amount, stage, close_date)
    if deal_id:
        _deal_cache[cache_key] = deal_id
    return deal_id


async def create_deal(
    contact_id: str,
    deal_name: str,
    amount: float = 0,
    stage: str = "closedwon",
    close_date: str = "",
) -> str | None:
    """Create a Deal in HubSpot and associate it with a Contact.

    Returns the deal ID or None.
    """
    if not close_date:
        close_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    properties: dict = {
        "dealname": deal_name,
        "dealstage": stage,
        "closedate": close_date,
        "pipeline": "default",
    }
    if amount:
        properties["amount"] = str(amount)

    url = f"{BASE_URL}/crm/v3/objects/deals"
    payload = {
        "properties": properties,
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 3,  # deal → contact
                    }
                ],
            }
        ],
    }

    client = _get_http()
    resp = await client.post(url, json=payload, headers=_headers())

    if resp.status_code not in (200, 201):
        logger.error("HubSpot create deal failed [%d]: %s", resp.status_code, resp.text[:200])
        return None

    deal_id = resp.json().get("id")
    logger.info("HubSpot created deal %s: %s", deal_id, deal_name)
    return deal_id
