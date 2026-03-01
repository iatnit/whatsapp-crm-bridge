"""Feishu (Lark) Bitable HTTP API client.

Directly calls Feishu Open API — no MCP dependency.
Handles token refresh, customer search/create, and follow-up record creation.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings
from app.utils.phone import normalize_phone

logger = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn/open-apis"

# ── Shared HTTP client (TCP connection reuse) ─────────────────────────
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    """Get or create shared httpx client for TCP connection reuse."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=15)
    return _http


# Token cache
_token: str = ""
_token_expires_at: float = 0
_token_lock = asyncio.Lock()


# ── Auth ─────────────────────────────────────────────────────────────

async def _get_tenant_token() -> str:
    """Get or refresh the tenant_access_token.

    Uses asyncio.Lock to prevent concurrent refreshes from parallel webhooks.
    """
    global _token, _token_expires_at

    # Fast path: token still valid (no lock needed)
    if _token and time.time() < _token_expires_at - 60:
        return _token

    async with _token_lock:
        # Double-check after acquiring lock
        if _token and time.time() < _token_expires_at - 60:
            return _token

        client = _get_http()
        resp = await client.post(
            f"{BASE_URL}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token error: {data}")

        _token = data["tenant_access_token"]
        _token_expires_at = time.time() + data.get("expire", 7200)
        logger.info("Feishu token refreshed, expires in %ds", data.get("expire", 7200))
        return _token


async def _headers() -> dict:
    token = await _get_tenant_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Generic Bitable helpers ──────────────────────────────────────────

async def _search_records(
    table_id: str,
    field_name: str,
    value: str,
    app_token: str | None = None,
) -> list[dict]:
    """Search records in a Bitable table by field value (contains)."""
    app_token = app_token or settings.feishu_app_token
    url = f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"

    payload = {
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": field_name,
                    "operator": "contains",
                    "value": [value],
                }
            ],
        },
        "automatic_fields": True,
    }

    client = _get_http()
    resp = await client.post(url, json=payload, headers=await _headers())
    data = resp.json()

    if data.get("code") != 0:
        logger.error("Feishu search error: %s", data.get("msg"))
        return []

    items = data.get("data", {}).get("items", [])
    logger.debug("Search '%s' in %s: %d results", value, table_id, len(items))
    return items


async def _create_record(
    table_id: str,
    fields: dict,
    app_token: str | None = None,
) -> dict | None:
    """Create a record in a Bitable table."""
    app_token = app_token or settings.feishu_app_token
    url = f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records"

    client = _get_http()
    resp = await client.post(
        url,
        json={"fields": fields},
        headers=await _headers(),
    )
    data = resp.json()

    if data.get("code") != 0:
        logger.error("Feishu create error in %s: %s", table_id, data.get("msg"))
        return None

    record = data.get("data", {}).get("record", {})
    record_id = record.get("record_id", "")
    logger.info("Created record %s in %s", record_id, table_id)
    return record


async def _update_record(
    table_id: str,
    record_id: str,
    fields: dict,
    app_token: str | None = None,
) -> dict | None:
    """Update an existing record in a Bitable table."""
    app_token = app_token or settings.feishu_app_token
    url = f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"

    client = _get_http()
    resp = await client.put(
        url,
        json={"fields": fields},
        headers=await _headers(),
    )
    data = resp.json()

    if data.get("code") != 0:
        logger.error("Feishu update error in %s/%s: %s", table_id, record_id, data.get("msg"))
        return None

    record = data.get("data", {}).get("record", {})
    logger.info("Updated record %s in %s", record_id, table_id)
    return record


# ── Customer CRM operations ─────────────────────────────────────────

async def search_customer(customer_name: str) -> str | None:
    """Search 客户管理CRM for a customer by name.

    Returns the record_id if found, None otherwise.
    Also caches the Feishu 编号 for cross-system sync.
    """
    items = await _search_records(
        table_id=settings.feishu_table_customers,
        field_name="客户",
        value=customer_name,
    )
    if items:
        record_id = items[0].get("record_id", "")
        _extract_customer_number(items, record_id)
        logger.info("Found customer '%s' → %s", customer_name, record_id)
        return record_id
    return None


async def search_customer_by_phone(phone: str) -> str | None:
    """Search 客户管理CRM for a customer by phone number.

    Returns the record_id if found, None otherwise.
    More reliable than name-based search for dedup.
    Also caches the Feishu 编号 for cross-system sync.
    """
    normalized = normalize_phone(phone)

    items = await _search_records(
        table_id=settings.feishu_table_customers,
        field_name="联系电话",
        value=normalized,
    )
    if items:
        record_id = items[0].get("record_id", "")
        _extract_customer_number(items, record_id)
        logger.info("Found customer by phone '%s' → %s", normalized, record_id)
        return record_id
    return None


async def create_customer(
    name: str,
    contact: str = "",
    location: str = "",
    source: str = "WhatsApp",
    contact_person: str = "",
) -> str | None:
    """Create a new customer in 客户管理CRM.

    Returns the new record_id or None.
    Also caches the auto-generated Feishu 编号 for cross-system sync.
    """
    fields: dict = {"客户": name}
    if contact:
        fields["联系电话"] = normalize_phone(contact)
    if contact_person:
        fields["联系人"] = contact_person
    if location:
        fields["国家地区"] = location
    if source:
        fields["客户来源"] = source

    record = await _create_record(settings.feishu_table_customers, fields)
    if record:
        record_id = record.get("record_id", "")
        _extract_customer_number(record, record_id)
        return record_id
    return None


# Dedup lock and cache for ensure_customer
_customer_lock = asyncio.Lock()
_customer_cache: dict[str, str] = {}  # lowercase name → record_id
_phone_cache: dict[str, str] = {}     # phone → record_id

# Feishu 编号 cache: record_id → 6-digit customer number (e.g. "100008")
_customer_number_cache: dict[str, str] = {}

# Dedup cache for ensure_followup (key: "name|YYYY-MM-DD" → record_id)
_followup_cache: dict[str, str] = {}

# Attachment cache: same key → list of {"file_token": "xxx"} already written
_attachment_cache: dict[str, list[dict]] = {}


def _extract_customer_number(record: dict | list, record_id: str = "") -> str:
    """Extract 编号 from Feishu record fields and cache it.

    Works with either a single record dict or a list of search result items.
    Skips 编号=100 (unassigned placeholder).
    """
    if isinstance(record, list):
        if not record:
            return ""
        item = record[0]
    else:
        item = record

    fields = item.get("fields", {})
    number = fields.get("编号", "")
    if isinstance(number, (int, float)):
        number = str(int(number))
    number = str(number).strip() if number else ""

    rid = record_id or item.get("record_id", "")
    if rid and number and number != "100":
        _customer_number_cache[rid] = number
    return number


def get_customer_number(record_id: str) -> str:
    """Get cached Feishu 编号 for a customer record. Returns '' if not cached."""
    return _customer_number_cache.get(record_id, "")


def clear_customer_cache():
    """Clear the in-memory customer and followup caches. Call at pipeline start."""
    _customer_cache.clear()
    _phone_cache.clear()
    _customer_number_cache.clear()
    _followup_cache.clear()
    _attachment_cache.clear()


async def ensure_customer(
    name: str, phone: str = "", location: str = "",
    contact_person: str = "",
) -> str | None:
    """Search for a customer; create if not found. Returns record_id.

    Dedup priority: phone number first (reliable), then name (fallback).
    Uses a lock + in-memory cache to prevent concurrent duplicate creation.
    """
    cache_key = name.strip().lower()
    phone_key = normalize_phone(phone) if phone else ""

    # Fast path: check caches without lock
    if phone_key and phone_key in _phone_cache:
        return _phone_cache[phone_key]
    if cache_key in _customer_cache:
        return _customer_cache[cache_key]

    async with _customer_lock:
        # Double-check after acquiring lock
        if phone_key and phone_key in _phone_cache:
            return _phone_cache[phone_key]
        if cache_key in _customer_cache:
            return _customer_cache[cache_key]

        # 1. Search by phone first (most reliable dedup)
        record_id = None
        if phone:
            record_id = await search_customer_by_phone(phone)

        # 2. Fall back to name search
        if not record_id:
            record_id = await search_customer(name)

        if record_id:
            _customer_cache[cache_key] = record_id
            if phone_key:
                _phone_cache[phone_key] = record_id
            return record_id

        # 3. Create new customer
        logger.info("Customer '%s' (%s) not found, creating new record", name, phone)
        record_id = await create_customer(
            name, contact=phone, location=location,
            contact_person=contact_person,
        )
        if record_id:
            _customer_cache[cache_key] = record_id
            if phone_key:
                _phone_cache[phone_key] = record_id
        return record_id


# ── Follow-up record ────────────────────────────────────────────────

async def create_followup(
    customer_record_id: str,
    title: str,
    detail: str,
    summary: str = "",
    method: str = "WhatsApp沟通",
    attachments: list[dict] | None = None,
) -> str | None:
    """Create a follow-up record in 客户跟进记录.

    Args:
        customer_record_id: record_id from 客户管理CRM (for DuplexLink)
        title: short title (跟进内容)
        detail: detailed notes (跟进情况)
        summary: one-liner (总结)
        method: follow-up method (跟进形式)
        attachments: list of {"file_token": "xxx"} for Bitable attachment field

    Returns the new record_id or None.
    """
    fields = {
        "跟进内容": title,
        "客户名称": [customer_record_id],  # DuplexLink expects a list
        "跟进形式": method,
        "跟进情况": detail,
    }
    if summary:
        fields["总结"] = summary
    if attachments:
        fields["附件"] = attachments

    record = await _create_record(settings.feishu_table_followup, fields)
    return record.get("record_id") if record else None


async def search_today_followup(customer_name: str) -> dict | None:
    """Search for a follow-up record created today for the given customer.

    Returns the record dict (with record_id and fields) if found, None otherwise.
    """
    items = await _search_records(
        table_id=settings.feishu_table_followup,
        field_name="客户名称",
        value=customer_name,
    )
    if not items:
        return None

    # Filter by today's date (CST, UTC+8)
    cst = timezone(timedelta(hours=8))
    today_start = datetime.now(cst).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)

    for item in items:
        fields = item.get("fields", {})
        # CreatedTime auto-field "跟进时间" returns ms timestamp
        created_ts = fields.get("跟进时间")
        if isinstance(created_ts, (int, float)) and created_ts >= today_start_ms:
            return item

    return None


async def ensure_followup(
    customer_record_id: str,
    customer_name: str,
    title: str,
    detail: str,
    summary: str = "",
    method: str = "WhatsApp沟通",
    image_paths: list[str] | None = None,
) -> str | None:
    """Create or update a follow-up record (max 1 per customer per day).

    If a followup already exists for this customer today, appends to it.
    Otherwise creates a new record.
    Uses both in-memory cache and Feishu search for dedup.

    Args:
        image_paths: list of local file paths to upload as attachments.
    """
    cst = timezone(timedelta(hours=8))
    today_str = datetime.now(cst).strftime("%Y-%m-%d")
    cache_key = f"{customer_name.strip().lower()}|{today_str}"

    # Upload images to Feishu if provided
    attachments = None
    if image_paths:
        try:
            from app.writers.feishu_uploader import upload_files_for_bitable
            token = await _get_tenant_token()
            attachments = await upload_files_for_bitable(
                image_paths, token, settings.feishu_app_token
            )
            if attachments:
                logger.info("Uploaded %d images for %s", len(attachments), customer_name)
        except Exception as e:
            logger.error("Image upload failed for %s: %s", customer_name, e)

    # Check in-memory cache first
    cached_record_id = _followup_cache.get(cache_key)
    if cached_record_id:
        # Already have a record today — update it
        logger.info("Followup cache hit for %s, updating %s", customer_name, cached_record_id)
        update_fields = {
            "跟进内容": title,
            "跟进情况": detail,
            "总结": summary or "",
        }
        if attachments:
            # Merge with locally cached attachments — no extra API call
            old_attachments = _attachment_cache.get(cache_key, [])
            merged = old_attachments + attachments
            update_fields["附件"] = merged
            _attachment_cache[cache_key] = merged
        result = await _update_record(
            settings.feishu_table_followup, cached_record_id, update_fields
        )
        return cached_record_id if result else None

    # Cache miss — search Feishu
    existing = await search_today_followup(customer_name)

    if existing:
        record_id = existing.get("record_id", "")
        old_fields = existing.get("fields", {})
        old_detail = old_fields.get("跟进情况", "") or ""
        old_summary = old_fields.get("总结", "") or ""

        # Append new detail with separator
        new_detail = f"{old_detail}\n\n---\n\n{detail}" if old_detail else detail
        new_summary = summary or old_summary

        update_fields = {
            "跟进内容": title,
            "跟进情况": new_detail,
            "总结": new_summary,
        }
        if attachments:
            # Merge with existing attachments
            old_attachments = old_fields.get("附件", []) or []
            merged = old_attachments + attachments
            update_fields["附件"] = merged
            _attachment_cache[cache_key] = merged

        result = await _update_record(
            settings.feishu_table_followup, record_id, update_fields
        )
        if result:
            _followup_cache[cache_key] = record_id
            logger.info("Updated existing followup %s for %s", record_id, customer_name)
            return record_id
        return None

    # No existing record today — create new
    record_id = await create_followup(
        customer_record_id=customer_record_id,
        title=title,
        detail=detail,
        summary=summary,
        method=method,
        attachments=attachments,
    )
    if record_id:
        _followup_cache[cache_key] = record_id
        if attachments:
            _attachment_cache[cache_key] = attachments
    return record_id
