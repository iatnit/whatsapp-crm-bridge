"""Upsert per-customer profile pages in a Notion database.

Each customer gets one page.  The page body has two sections:
  1. 「手动备注」 — a block the AI never touches (created once, left alone).
  2. AI跟进记录 — appended each run with a timestamped analysis block.

Database properties updated on every run:
  - 客户名 (title)
  - 电话
  - 地区
  - 产品兴趣
  - 客户阶段
  - 最后联系 (date)
  - 消息数
"""

import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_MAX_TEXT_LEN = 1990  # Notion rich_text chunk limit


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rt(text: str) -> list[dict]:
    """Split long text into ≤2000-char Notion rich_text chunks."""
    chunks, remaining = [], text or ""
    while remaining:
        chunks.append({"text": {"content": remaining[:_MAX_TEXT_LEN]}})
        remaining = remaining[_MAX_TEXT_LEN:]
    return chunks or [{"text": {"content": ""}}]


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rt(text)}}


def _heading2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rt(text)}}


def _heading3(text: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": _rt(text)}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt(text)}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _build_ai_block(analysis: dict, date_str: str) -> list[dict]:
    """Return a list of Notion blocks for one AI analysis run."""
    blocks: list[dict] = []
    blocks.append(_heading3(f"AI分析 — {date_str}"))

    summary = analysis.get("summary", "")
    if summary:
        blocks.append(_bullet(f"摘要: {summary}"))

    demand = analysis.get("demand_summary", "")
    if demand:
        blocks.append(_bullet(f"需求: {demand}"))

    next_actions = analysis.get("next_actions", {})
    if isinstance(next_actions, dict):
        today = next_actions.get("today", "")
        tomorrow = next_actions.get("tomorrow", "")
        pending = next_actions.get("pending_customer", "")
        if today:
            blocks.append(_bullet(f"今天: {today}"))
        if tomorrow:
            blocks.append(_bullet(f"明天: {tomorrow}"))
        if pending:
            blocks.append(_bullet(f"等待客户: {pending}"))

    codes = analysis.get("recommended_codes", [])
    if codes:
        blocks.append(_bullet(f"推荐编码: {', '.join(codes)}"))

    followup = analysis.get("followup_detail", "")
    if followup:
        blocks.append(_paragraph(followup))

    blocks.append(_divider())
    return blocks


async def _find_customer_page(
    client: httpx.AsyncClient, customer_name: str
) -> str | None:
    """Return page_id of existing page matching 客户名, or None."""
    resp = await client.post(
        f"{_NOTION_API}/databases/{settings.notion_customer_db_id}/query",
        headers=_headers(),
        json={
            "filter": {
                "property": "客户名",
                "title": {"equals": customer_name},
            },
            "page_size": 1,
        },
    )
    if resp.status_code != 200:
        logger.debug("Notion customer query failed: %d %s", resp.status_code, resp.text[:200])
        return None
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


async def _create_customer_page(
    client: httpx.AsyncClient,
    customer_name: str,
    phone: str,
    location: str,
    products: list[str],
    stage: str,
    total_messages: int,
    date_str: str,
    ai_blocks: list[dict],
) -> str | None:
    """Create a new customer page with manual notes section + first AI block."""
    date_ms = _today_midnight_ms()

    initial_blocks = [
        _heading2("手动备注"),
        _paragraph("在此记录手动备注，AI不会修改此区域。"),
        _divider(),
        _heading2("AI跟进记录"),
    ] + ai_blocks

    resp = await client.post(
        f"{_NOTION_API}/pages",
        headers=_headers(),
        json={
            "parent": {"database_id": settings.notion_customer_db_id},
            "properties": _build_properties(
                customer_name, phone, location, products, stage, total_messages, date_ms
            ),
            "children": initial_blocks[:95],
        },
    )
    if resp.status_code != 200:
        logger.error("Notion create customer page failed: %d %s", resp.status_code, resp.text[:300])
        return None

    page_id = resp.json().get("id", "")

    # Append any remaining blocks
    remaining = initial_blocks[95:]
    while remaining:
        await client.patch(
            f"{_NOTION_API}/blocks/{page_id}/children",
            headers=_headers(),
            json={"children": remaining[:95]},
        )
        remaining = remaining[95:]

    return page_id


async def _update_customer_properties(
    client: httpx.AsyncClient,
    page_id: str,
    customer_name: str,
    phone: str,
    location: str,
    products: list[str],
    stage: str,
    total_messages: int,
) -> None:
    """Update database properties on an existing customer page."""
    date_ms = _today_midnight_ms()
    await client.patch(
        f"{_NOTION_API}/pages/{page_id}",
        headers=_headers(),
        json={"properties": _build_properties(
            customer_name, phone, location, products, stage, total_messages, date_ms
        )},
    )


async def _append_ai_block(
    client: httpx.AsyncClient, page_id: str, ai_blocks: list[dict]
) -> None:
    """Append AI analysis blocks to the end of an existing page."""
    remaining = list(ai_blocks)
    while remaining:
        await client.patch(
            f"{_NOTION_API}/blocks/{page_id}/children",
            headers=_headers(),
            json={"children": remaining[:95]},
        )
        remaining = remaining[95:]


def _today_midnight_ms() -> int:
    cst = timezone(timedelta(hours=8))
    today = datetime.now(cst).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(today.timestamp() * 1000)


def _build_properties(
    customer_name: str,
    phone: str,
    location: str,
    products: list[str],
    stage: str,
    total_messages: int,
    date_ms: int,
) -> dict:
    props: dict = {
        "客户名": {"title": [{"text": {"content": customer_name or phone}}]},
    }
    if phone:
        props["电话"] = {"rich_text": [{"text": {"content": phone}}]}
    if location:
        props["地区"] = {"rich_text": [{"text": {"content": location}}]}
    if products:
        props["产品兴趣"] = {"rich_text": [{"text": {"content": ", ".join(products)}}]}
    # 客户阶段 is included in the AI block body; skip as DB property to avoid missing-column errors
    if total_messages:
        props["消息数"] = {"number": total_messages}
    props["最后联系"] = {"date": {"start": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")}}
    return props


async def upsert_customer_profile(
    customer_name: str,
    phone: str,
    location: str,
    analysis: dict,
    total_messages: int,
) -> str | None:
    """Upsert a Notion customer profile page.

    Creates the page on first call; updates properties and appends a new
    AI analysis block on subsequent calls. Returns page_id or None.
    """
    if not settings.notion_token or not settings.notion_customer_db_id:
        return None

    cst = timezone(timedelta(hours=8))
    date_str = datetime.now(cst).strftime("%Y-%m-%d")

    products = analysis.get("recommended_codes", [])
    stage = ""
    tags = analysis.get("tags", [])
    for t in tags:
        if t.startswith("stage/"):
            stage = t.split("/", 1)[1]
            break

    ai_blocks = _build_ai_block(analysis, date_str)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            page_id = await _find_customer_page(client, customer_name or phone)
            if page_id:
                await _update_customer_properties(
                    client, page_id, customer_name, phone, location,
                    products, stage, total_messages,
                )
                await _append_ai_block(client, page_id, ai_blocks)
                logger.info("Notion customer profile updated: %s (%s)", customer_name, page_id)
            else:
                page_id = await _create_customer_page(
                    client, customer_name, phone, location,
                    products, stage, total_messages, date_str, ai_blocks,
                )
                if page_id:
                    logger.info("Notion customer profile created: %s (%s)", customer_name, page_id)
    except Exception as e:
        logger.error("Notion customer upsert failed for %s: %s", customer_name, e)
        return None

    return page_id
