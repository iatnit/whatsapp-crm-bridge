"""Write daily CEO report to a Notion database."""

import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_MAX_BLOCKS_PER_REQUEST = 95   # Notion limit is 100; stay safe
_MAX_TEXT_LEN = 1990           # Notion rich text limit is 2000


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich_text(content: str) -> list[dict]:
    """Split long text into ≤2000-char chunks for Notion rich_text."""
    chunks = []
    while content:
        chunks.append({"text": {"content": content[:_MAX_TEXT_LEN]}})
        content = content[_MAX_TEXT_LEN:]
    return chunks or [{"text": {"content": ""}}]


def _md_to_blocks(md: str) -> list[dict]:
    """Convert markdown report to Notion block objects."""
    blocks = []
    for line in md.splitlines():
        if line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": _rich_text(line[2:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _rich_text(line[3:])}})
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _rich_text(line[4:])}})
        elif line.startswith("- "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rich_text(line[2:])}})
        elif line.strip() == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif line.strip():
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _rich_text(line)}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": []}})
    return blocks


async def _query_page_by_date(client: httpx.AsyncClient, date_str: str) -> str | None:
    """Return existing page_id for today's date, or None."""
    resp = await client.post(
        f"{_NOTION_API}/databases/{settings.notion_report_db_id}/query",
        headers=_headers(),
        json={"filter": {"property": "日期", "title": {"equals": date_str}}},
    )
    if resp.status_code != 200:
        logger.debug("Notion query failed: %d %s", resp.status_code, resp.text[:200])
        return None
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


async def _archive_page(client: httpx.AsyncClient, page_id: str) -> None:
    """Archive (soft-delete) an existing page."""
    await client.patch(
        f"{_NOTION_API}/pages/{page_id}",
        headers=_headers(),
        json={"archived": True},
    )


async def _create_page(
    client: httpx.AsyncClient, date_str: str, blocks: list[dict]
) -> str | None:
    """Create a new page in the report database with the given blocks."""
    # Notion allows max 100 blocks in create; send first batch
    first_batch = blocks[:_MAX_BLOCKS_PER_REQUEST]
    resp = await client.post(
        f"{_NOTION_API}/pages",
        headers=_headers(),
        json={
            "parent": {"database_id": settings.notion_report_db_id},
            "properties": {
                "日期": {"title": [{"text": {"content": date_str}}]},
            },
            "children": first_batch,
        },
    )
    if resp.status_code != 200:
        logger.error("Notion create page failed: %d %s", resp.status_code, resp.text[:300])
        return None

    page_id = resp.json().get("id", "")

    # Append remaining blocks in chunks
    remaining = blocks[_MAX_BLOCKS_PER_REQUEST:]
    while remaining:
        chunk = remaining[:_MAX_BLOCKS_PER_REQUEST]
        remaining = remaining[_MAX_BLOCKS_PER_REQUEST:]
        await client.patch(
            f"{_NOTION_API}/blocks/{page_id}/children",
            headers=_headers(),
            json={"children": chunk},
        )

    return page_id


async def write_report_to_notion(report: str, summary: dict) -> str | None:
    """Write daily CEO report to Notion.

    Upserts by date: archives any existing page for today, then creates fresh.
    Returns the new page_id or None on failure.
    """
    if not settings.notion_token or not settings.notion_report_db_id:
        return None

    cst = timezone(timedelta(hours=8))
    date_str = datetime.now(cst).strftime("%Y-%m-%d")
    blocks = _md_to_blocks(report)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Archive existing page for today if any
            existing_id = await _query_page_by_date(client, date_str)
            if existing_id:
                await _archive_page(client, existing_id)
                logger.debug("Notion: archived old report %s", existing_id)

            page_id = await _create_page(client, date_str, blocks)
            if page_id:
                logger.info("Notion CEO日报 written for %s (page %s)", date_str, page_id)
                return page_id
    except Exception as e:
        logger.error("Notion write_report failed: %s", e)

    return None
