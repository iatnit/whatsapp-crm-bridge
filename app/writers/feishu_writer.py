"""Feishu (Lark) Bitable HTTP API client.

Directly calls Feishu Open API — no MCP dependency.
Handles token refresh, customer search/create, and follow-up record creation.
"""

import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn/open-apis"

# Token cache
_token: str = ""
_token_expires_at: float = 0


# ── Auth ─────────────────────────────────────────────────────────────

async def _get_tenant_token() -> str:
    """Get or refresh the tenant_access_token."""
    global _token, _token_expires_at

    if _token and time.time() < _token_expires_at - 60:
        return _token

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{BASE_URL}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
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

    async with httpx.AsyncClient(timeout=15) as client:
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

    async with httpx.AsyncClient(timeout=15) as client:
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


# ── Customer CRM operations ─────────────────────────────────────────

async def search_customer(customer_name: str) -> str | None:
    """Search 客户管理CRM for a customer by name.

    Returns the record_id if found, None otherwise.
    """
    items = await _search_records(
        table_id=settings.feishu_table_customers,
        field_name="客户",
        value=customer_name,
    )
    if items:
        record_id = items[0].get("record_id", "")
        logger.info("Found customer '%s' → %s", customer_name, record_id)
        return record_id
    return None


async def create_customer(
    name: str,
    contact: str = "",
    location: str = "",
    source: str = "WhatsApp",
) -> str | None:
    """Create a new customer in 客户管理CRM.

    Returns the new record_id or None.
    """
    fields: dict = {"客户": name}
    if contact:
        fields["联系电话"] = contact
    if location:
        fields["国家地区"] = location
    if source:
        fields["客户来源"] = source

    record = await _create_record(settings.feishu_table_customers, fields)
    return record.get("record_id") if record else None


async def ensure_customer(
    name: str, phone: str = "", location: str = ""
) -> str | None:
    """Search for a customer; create if not found. Returns record_id."""
    record_id = await search_customer(name)
    if record_id:
        return record_id

    logger.info("Customer '%s' not found, creating new record", name)
    return await create_customer(name, contact=phone, location=location)


# ── Follow-up record ────────────────────────────────────────────────

async def create_followup(
    customer_record_id: str,
    title: str,
    detail: str,
    summary: str = "",
    method: str = "WhatsApp沟通",
) -> str | None:
    """Create a follow-up record in 客户跟进记录.

    Args:
        customer_record_id: record_id from 客户管理CRM (for DuplexLink)
        title: short title (跟进内容)
        detail: detailed notes (跟进情况)
        summary: one-liner (总结)
        method: follow-up method (跟进形式)

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

    record = await _create_record(settings.feishu_table_followup, fields)
    return record.get("record_id") if record else None
