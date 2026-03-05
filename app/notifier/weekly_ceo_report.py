"""Generate CEO weekly report from Feishu daily reports using AI summarization.

Workflow:
1. Fetch last 7 days of CEO日报 from Feishu
2. Send to Gemini for structured weekly summary
3. Upsert into Feishu CEO周报 table (auto-created on first run)
4. Also send a summary card via Feishu webhook
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn/open-apis"
CEO_APP_TOKEN = "OPNSb3Y9la0gaAs1uN9cYAejnNd"
CEO_DAILY_TABLE_ID = "tbls91RzscIQkMv4"
CST = timezone(timedelta(hours=8))

# Cached weekly table ID (set after first ensure_weekly_table call)
_weekly_table_id: str | None = None


# ── Feishu auth ────────────────────────────────────────────────────────

async def _get_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
    )
    return resp.json().get("tenant_access_token", "")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Feishu table management ────────────────────────────────────────────

async def _list_tables(client: httpx.AsyncClient, token: str) -> list[dict]:
    resp = await client.get(
        f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables",
        headers=_auth(token),
    )
    return resp.json().get("data", {}).get("items", [])


async def _create_weekly_table(client: httpx.AsyncClient, token: str) -> str:
    """Create CEO周报 table in the Feishu base. Returns table_id."""
    resp = await client.post(
        f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables",
        headers=_auth(token),
        json={
            "table": {
                "name": "CEO周报",
                "fields": [
                    {"field_name": "周期",   "type": 1},   # title text, e.g. "2026-W10"
                    {"field_name": "日期范围", "type": 1},  # text, e.g. "03/02–03/08"
                    {"field_name": "周报全文", "type": 1},  # full markdown report
                    {"field_name": "本周亮点", "type": 1},  # highlights
                    {"field_name": "下周重点", "type": 1},  # next week focus
                ],
            }
        },
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to create CEO周报 table: {data.get('msg')}")
    table_id = data["data"]["table_id"]
    logger.info("Created CEO周报 table: %s", table_id)
    return table_id


async def ensure_weekly_table(client: httpx.AsyncClient, token: str) -> str:
    """Return CEO周报 table_id, creating the table if it doesn't exist."""
    global _weekly_table_id
    if _weekly_table_id:
        return _weekly_table_id

    tables = await _list_tables(client, token)
    for t in tables:
        if t.get("name") == "CEO周报":
            _weekly_table_id = t["table_id"]
            logger.info("Found existing CEO周报 table: %s", _weekly_table_id)
            return _weekly_table_id

    _weekly_table_id = await _create_weekly_table(client, token)
    return _weekly_table_id


# ── Fetch daily reports ────────────────────────────────────────────────

async def _fetch_daily_reports(client: httpx.AsyncClient, token: str, days: int = 7) -> list[dict]:
    """Fetch daily reports from Feishu CEO日报 for the past N days."""
    now = datetime.now(CST)
    cutoff_ms = int((now - timedelta(days=days)).timestamp() * 1000)

    payload = {
        "filter": {
            "conjunction": "and",
            "conditions": [{
                "field_name": "日期",
                "operator": "isGreater",
                "value": ["ExactDate", str(cutoff_ms)],
            }],
        },
        "automatic_fields": True,
        "page_size": 20,
    }

    resp = await client.post(
        f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{CEO_DAILY_TABLE_ID}/records/search",
        headers=_auth(token),
        json=payload,
    )
    data = resp.json()
    if data.get("code") != 0:
        logger.error("Failed to fetch daily reports: %s", data.get("msg"))
        return []

    items = data.get("data", {}).get("items", [])
    # Sort by date ascending
    def _ts(item):
        v = item.get("fields", {}).get("日期")
        if isinstance(v, dict):
            return v.get("value", 0)
        return v or 0

    items.sort(key=_ts)
    return items


# ── AI summary ─────────────────────────────────────────────────────────

_WEEKLY_SYSTEM = """你是 LOCACRYSTAL（义乌水钻饰品厂商）的首席数据助理。
根据本周的每日销售日报，为 CEO 生成一份结构清晰、重点突出的周报。
周报用中文撰写，格式为 Markdown，语言简练专业。"""

_WEEKLY_PROMPT = """以下是本周（{week_label}）的每日日报，共 {count} 天：

{daily_content}

---

请生成 CEO 周报，包含以下章节：

## 本周概况
（本周整体销售、客户沟通情况，2-3句总结）

## 重点客户动态
（本周最值得关注的3-5个客户，及其进展）

## 销售进展
（成单、打样、报价、谈判等重要进展，分条列出）

## 问题与风险
（本周遇到的投诉、异常、需要注意的风险点，没有则写"无"）

## 下周行动重点
（基于本周情况，下周需要重点跟进的客户和任务，3-5条）"""


async def _generate_ai_summary(daily_reports: list[dict], week_label: str) -> str:
    """Use Gemini to generate a structured weekly summary."""
    from app.llm.gemini import call_gemini

    # Build input content from daily reports
    parts = []
    for r in daily_reports:
        fields = r.get("fields", {})
        date_val = fields.get("日期")
        if isinstance(date_val, dict):
            ts_ms = date_val.get("value", 0)
        else:
            ts_ms = date_val or 0

        if ts_ms:
            date_str = datetime.fromtimestamp(ts_ms / 1000, tz=CST).strftime("%Y-%m-%d")
        else:
            date_str = "未知日期"

        report_text = fields.get("今日日报全文") or fields.get("客户相关") or ""
        if report_text:
            parts.append(f"### {date_str}\n{report_text[:3000]}")  # cap per day

    if not parts:
        return "本周暂无日报数据，无法生成周报。"

    daily_content = "\n\n".join(parts)
    prompt = _WEEKLY_PROMPT.format(
        week_label=week_label,
        count=len(parts),
        daily_content=daily_content,
    )

    result = await call_gemini(
        system_prompt=_WEEKLY_SYSTEM,
        user_prompt=prompt,
        temperature=0.4,
        max_tokens=3000,
        timeout=90,
    )
    return result or "AI 生成失败，请检查 Gemini API 配置。"


# ── Feishu write ───────────────────────────────────────────────────────

async def _upsert_weekly_record(
    client: httpx.AsyncClient,
    token: str,
    table_id: str,
    week_key: str,
    date_range: str,
    full_report: str,
    highlights: str,
    next_week: str,
) -> str | None:
    """Upsert weekly report record. Returns record_id."""
    # Search for existing record by 周期 (title field)
    search_resp = await client.post(
        f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{table_id}/records/search",
        headers=_auth(token),
        json={
            "filter": {
                "conjunction": "and",
                "conditions": [{
                    "field_name": "周期",
                    "operator": "is",
                    "value": [week_key],
                }],
            },
        },
    )
    items = search_resp.json().get("data", {}).get("items", [])

    fields = {
        "周期": week_key,
        "日期范围": date_range,
        "周报全文": full_report,
        "本周亮点": highlights,
        "下周重点": next_week,
    }

    if items:
        record_id = items[0]["record_id"]
        await client.put(
            f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{table_id}/records/{record_id}",
            headers=_auth(token),
            json={"fields": fields},
        )
        logger.info("CEO周报 updated: %s (%s)", week_key, record_id)
        return record_id
    else:
        resp = await client.post(
            f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{table_id}/records",
            headers=_auth(token),
            json={"fields": fields},
        )
        data = resp.json()
        if data.get("code") == 0:
            record_id = data["data"]["record"]["record_id"]
            logger.info("CEO周报 created: %s (%s)", week_key, record_id)
            return record_id
        logger.error("CEO周报 create failed: %s", data.get("msg"))
        return None


def _extract_section(report: str, heading: str) -> str:
    """Extract a section from the markdown report."""
    lines = report.splitlines()
    capturing = False
    result = []
    for line in lines:
        if line.strip().startswith("## ") and heading in line:
            capturing = True
            continue
        if capturing:
            if line.strip().startswith("## "):
                break
            result.append(line)
    return "\n".join(result).strip()


# ── Webhook notification ───────────────────────────────────────────────

async def _send_webhook_summary(week_label: str, report: str) -> None:
    if not settings.feishu_webhook_url:
        return
    # Extract highlights and next week sections for the card
    highlights = _extract_section(report, "亮点") or _extract_section(report, "进展")
    next_week = _extract_section(report, "下周")

    lines = [
        [{"tag": "text", "text": f"📊 {week_label} 周报已生成\n"}],
    ]
    if highlights:
        lines.append([{"tag": "text", "text": "🌟 本周亮点：\n" + highlights[:300]}])
    if next_week:
        lines.append([{"tag": "text", "text": "\n📌 下周重点：\n" + next_week[:300]}])
    lines.append([{"tag": "text", "text": "\n➡️ 完整周报请查看飞书 CEO周报多维表格"}])

    payload = {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {"title": f"📋 CEO周报 — {week_label}", "content": lines}}},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(settings.feishu_webhook_url, json=payload)
    except Exception as e:
        logger.error("Weekly report webhook failed: %s", e)


# ── Main entry point ───────────────────────────────────────────────────

async def run_weekly_ceo_report(days: int = 7) -> bool:
    """Fetch last N days of daily reports, generate AI summary, write to Feishu CEO周报.

    Returns True if successful.
    """
    now = datetime.now(CST)
    week_start = now - timedelta(days=days - 1)
    week_key = now.strftime("%Y-W%V")
    date_range = f"{week_start.strftime('%m/%d')}–{now.strftime('%m/%d')}"
    week_label = f"{week_start.strftime('%m/%d')} 至 {now.strftime('%m/%d')}"

    logger.info("Generating CEO weekly report for %s (%s days)", week_label, days)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _get_token(client)
            if not token:
                logger.error("Failed to get Feishu token")
                return False

            # Fetch daily reports
            daily_reports = await _fetch_daily_reports(client, token, days)
            logger.info("Fetched %d daily reports", len(daily_reports))

            if not daily_reports:
                logger.warning("No daily reports found for the past %d days", days)
                return False

            # Ensure weekly table exists
            table_id = await ensure_weekly_table(client, token)

            # Generate AI summary
            logger.info("Generating AI summary...")
            full_report = await _generate_ai_summary(daily_reports, week_label)

            # Extract sub-sections
            highlights = _extract_section(full_report, "亮点") or _extract_section(full_report, "进展")
            next_week = _extract_section(full_report, "下周")

            # Write to Feishu
            record_id = await _upsert_weekly_record(
                client, token, table_id,
                week_key=week_key,
                date_range=date_range,
                full_report=full_report,
                highlights=highlights,
                next_week=next_week,
            )

            if record_id:
                await _send_webhook_summary(week_label, full_report)
                logger.info("CEO weekly report done: %s → %s", week_key, record_id)
                return True

    except Exception:
        logger.exception("CEO weekly report failed")

    return False
