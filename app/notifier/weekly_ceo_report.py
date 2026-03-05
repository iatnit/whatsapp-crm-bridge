"""Generate CEO weekly report from multiple Feishu data sources using AI summarization.

Data sources (past 7 days):
- WhatsApp CRM: CEO日报 (daily pipeline summaries)
- 销售CRM: 客户跟进记录, 合同管理
- 收付款: 收款登记, 付款登记
- 发货登记: 客户发货登记

Output: Feishu CEO周报 table + Feishu webhook card
Schedule: Every Monday 09:00 CST
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn/open-apis"
CST = timezone(timedelta(hours=8))

# ── Feishu base / table IDs ────────────────────────────────────────────
CEO_APP_TOKEN    = "OPNSb3Y9la0gaAs1uN9cYAejnNd"
CEO_DAILY_TABLE  = "tbls91RzscIQkMv4"   # CEO日报

CRM_APP_TOKEN    = "XYeCby15ga5CDKsX57YcFL1Hnce"  # 销售CRM
CRM_FOLLOWUP     = "tblcftbYX7E0cEUo"   # 客户跟进记录
CRM_CONTRACT     = "tblzjbbv9JlGw8kz"   # 合同管理

PAY_APP_TOKEN    = "Uw9TbBWpqa1hZSsH8wqc8vwInSb"  # 收付款
PAY_RECEIPT      = "tblHE2LqIBszOasy"   # 收款登记
PAY_PAYMENT      = "tblDlgC8Pd1rZY0r"   # 付款登记

SHIP_APP_TOKEN   = "C4iwb20bpaEmWzsStBJc1JAXnuf"  # 发货登记
SHIP_TABLE       = "tblln6iQgVrkCmjO"   # 客户发货登记

_weekly_table_id: str | None = None


# ── Auth ───────────────────────────────────────────────────────────────

async def _get_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
    )
    return resp.json().get("tenant_access_token", "")


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Generic record fetcher ─────────────────────────────────────────────

async def _list_records(
    client: httpx.AsyncClient,
    token: str,
    app_token: str,
    table_id: str,
    page_size: int = 200,
) -> list[dict]:
    """List all records from a Feishu Bitable table (up to page_size)."""
    resp = await client.get(
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        headers=_h(token),
        params={"page_size": page_size, "automatic_fields": "true"},
    )
    data = resp.json()
    if data.get("code") != 0:
        logger.warning("Feishu list_records failed [%s/%s]: %s", app_token, table_id, data.get("msg"))
        return []
    return data.get("data", {}).get("items", [])


def _field_text(val) -> str:
    """Extract displayable text from a Feishu field value."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("name") or item.get("value") or "")
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p)
    if isinstance(val, dict):
        return val.get("text") or val.get("value") or str(val)
    return str(val)


def _field_num(val) -> float:
    """Extract numeric value from a Feishu field."""
    if isinstance(val, (int, float)):
        return float(val)
    return 0.0


def _field_ts(val) -> float:
    """Extract Unix timestamp (seconds) from a Feishu date/datetime field."""
    if isinstance(val, (int, float)):
        return float(val) / 1000  # Feishu stores ms
    return 0.0


# ── Fetch each data source ─────────────────────────────────────────────

async def _fetch_daily_reports(client: httpx.AsyncClient, token: str, cutoff_ts: float) -> list[dict]:
    """Fetch CEO日报 records for the past week."""
    items = await _list_records(client, token, CEO_APP_TOKEN, CEO_DAILY_TABLE)
    result = []
    for r in items:
        f = r.get("fields", {})
        date_val = f.get("日期")
        ts = _field_ts(date_val) if date_val else 0
        if ts >= cutoff_ts:
            result.append({
                "date": datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%d") if ts else "",
                "report": _field_text(f.get("今日日报全文", ""))[:4000],
                "customers": _field_text(f.get("客户相关", "")),
            })
    result.sort(key=lambda x: x["date"])
    return result


async def _fetch_followups(client: httpx.AsyncClient, token: str, cutoff_ts: float) -> list[dict]:
    """Fetch 客户跟进记录 for the past week."""
    items = await _list_records(client, token, CRM_APP_TOKEN, CRM_FOLLOWUP)
    result = []
    for r in items:
        f = r.get("fields", {})
        ts = _field_ts(f.get("跟进时间", 0))
        if ts >= cutoff_ts:
            result.append({
                "time": datetime.fromtimestamp(ts, tz=CST).strftime("%m/%d") if ts else "",
                "customer": _field_text(f.get("客户名称", "")),
                "content": _field_text(f.get("跟进内容", "")),
                "summary": _field_text(f.get("总结", "")),
                "type": _field_text(f.get("跟进形式", "")),
            })
    return result


async def _fetch_contracts(client: httpx.AsyncClient, token: str, cutoff_ts: float) -> list[dict]:
    """Fetch 合同管理 records signed this week."""
    items = await _list_records(client, token, CRM_APP_TOKEN, CRM_CONTRACT)
    result = []
    for r in items:
        f = r.get("fields", {})
        ts = _field_ts(f.get("签约日期", 0))
        if ts >= cutoff_ts:
            result.append({
                "no": _field_text(f.get("合同编号", "")),
                "customer": _field_text(f.get("客户名称", "")),
                "amount": _field_num(f.get("合同金额", 0)),
                "date": datetime.fromtimestamp(ts, tz=CST).strftime("%m/%d") if ts else "",
            })
    return result


async def _fetch_receipts(client: httpx.AsyncClient, token: str, cutoff_ts: float) -> list[dict]:
    """Fetch 收款登记 for the past week."""
    items = await _list_records(client, token, PAY_APP_TOKEN, PAY_RECEIPT)
    result = []
    for r in items:
        f = r.get("fields", {})
        ts = _field_ts(f.get("日期", 0))
        if ts >= cutoff_ts:
            result.append({
                "customer": _field_text(f.get("客户", "")),
                "amount": _field_num(f.get("收款金额", 0)),
                "method": _field_text(f.get("收款方式", "")),
                "date": datetime.fromtimestamp(ts, tz=CST).strftime("%m/%d") if ts else "",
            })
    return result


async def _fetch_payments(client: httpx.AsyncClient, token: str, cutoff_ts: float) -> list[dict]:
    """Fetch 付款登记 for the past week."""
    items = await _list_records(client, token, PAY_APP_TOKEN, PAY_PAYMENT)
    result = []
    for r in items:
        f = r.get("fields", {})
        ts = _field_ts(f.get("日期", 0))
        if ts >= cutoff_ts:
            result.append({
                "supplier": _field_text(f.get("供应商", "")),
                "amount": _field_num(f.get("实际付款金额", 0)),
                "date": datetime.fromtimestamp(ts, tz=CST).strftime("%m/%d") if ts else "",
            })
    return result


async def _fetch_shipments(client: httpx.AsyncClient, token: str, cutoff_ts: float) -> list[dict]:
    """Fetch 客户发货登记 for the past week."""
    items = await _list_records(client, token, SHIP_APP_TOKEN, SHIP_TABLE)
    result = []
    for r in items:
        f = r.get("fields", {})
        ts = _field_ts(f.get("发货日期", 0))
        if ts >= cutoff_ts:
            result.append({
                "customer": _field_text(f.get("客户唛头或名称", "")),
                "pieces": _field_num(f.get("件数", 0)),
                "weight": _field_num(f.get("重量", 0)),
                "amount": _field_num(f.get("货款金额", 0)),
                "date": datetime.fromtimestamp(ts, tz=CST).strftime("%m/%d") if ts else "",
            })
    return result


# ── Build prompt context ───────────────────────────────────────────────

def _build_context(
    daily_reports: list[dict],
    followups: list[dict],
    contracts: list[dict],
    receipts: list[dict],
    payments: list[dict],
    shipments: list[dict],
    week_label: str,
) -> str:
    lines = [f"# 本周数据汇总（{week_label}）\n"]

    # Daily reports
    lines.append("## WhatsApp 客户沟通日报摘要")
    if daily_reports:
        for r in daily_reports:
            lines.append(f"### {r['date']}")
            if r["customers"]:
                lines.append(r["customers"][:800])
    else:
        lines.append("本周无日报记录")

    # Follow-ups
    lines.append(f"\n## 客户跟进记录（共{len(followups)}条）")
    if followups:
        for f in followups[:30]:
            customer = f["customer"] or "未知客户"
            content = (f["content"] or f["summary"])[:100]
            lines.append(f"- {f['time']} {customer}：{content}")
    else:
        lines.append("本周无跟进记录")

    # Contracts
    total_contract_amount = sum(c["amount"] for c in contracts)
    lines.append(f"\n## 合同管理（本周新签{len(contracts)}份，合计{total_contract_amount:,.0f}元）")
    if contracts:
        for c in contracts:
            lines.append(f"- {c['date']} {c['customer']} 合同号:{c['no']} 金额:{c['amount']:,.0f}元")
    else:
        lines.append("本周无新签合同")

    # Receipts
    total_receipt = sum(r["amount"] for r in receipts)
    lines.append(f"\n## 收款登记（共{len(receipts)}笔，合计{total_receipt:,.0f}元）")
    if receipts:
        for r in receipts[:20]:
            lines.append(f"- {r['date']} {r['customer']} {r['amount']:,.0f}元 ({r['method']})")
    else:
        lines.append("本周无收款记录")

    # Payments
    total_payment = sum(p["amount"] for p in payments)
    lines.append(f"\n## 付款登记（共{len(payments)}笔，合计{total_payment:,.0f}元）")
    if payments:
        for p in payments[:20]:
            lines.append(f"- {p['date']} {p['supplier']} {p['amount']:,.0f}元")
    else:
        lines.append("本周无付款记录")

    # Shipments
    total_ship_amount = sum(s["amount"] for s in shipments)
    total_pieces = sum(s["pieces"] for s in shipments)
    lines.append(f"\n## 发货登记（共{len(shipments)}票，{total_pieces:.0f}件，货款{total_ship_amount:,.0f}元）")
    if shipments:
        for s in shipments[:20]:
            lines.append(f"- {s['date']} {s['customer']} {s['pieces']:.0f}件 {s['weight']:.1f}kg 货款:{s['amount']:,.0f}元")
    else:
        lines.append("本周无发货记录")

    return "\n".join(lines)


# ── AI generation ──────────────────────────────────────────────────────

_SYSTEM = """你是 LOCACRYSTAL（义乌水钻饰品厂商）的首席数据助理。
根据本周的多维度业务数据，为 CEO 生成一份结构清晰、数据精准的中文周报。
语言简洁专业，突出关键数字和风险，格式为 Markdown。"""

_PROMPT = """以下是本周（{week_label}）的完整业务数据：

{context}

---

请基于以上数据生成 CEO 周报，包含以下章节：

## 本周概况
（一段话总结本周整体经营情况，突出关键数字：跟进客户数、新签合同、收款、发货等）

## 销售进展
（本周客户跟进重点、商机进展、新签合同详情，分条列出）

## 资金流水
（本周收款合计、付款合计、净流入，列出重要收付款明细）

## 发货情况
（本周发货票数、件数、货款总额，重要客户发货情况）

## 问题与风险
（本周遇到的客户投诉、付款逾期、发货延误等异常，没有则写"无异常"）

## 下周重点
（根据本周情况，下周需要重点跟进的客户、待签合同、待收款项，3-5条）"""


async def _generate_summary(context: str, week_label: str) -> str:
    from app.llm.gemini import call_gemini
    result = await call_gemini(
        system_prompt=_SYSTEM,
        user_prompt=_PROMPT.format(week_label=week_label, context=context),
        temperature=0.4,
        max_tokens=4000,
        timeout=120,
    )
    return result or "AI 生成失败，请检查 Gemini API 配置。"


# ── Feishu CEO周报 table ───────────────────────────────────────────────

async def _ensure_weekly_table(client: httpx.AsyncClient, token: str) -> str:
    global _weekly_table_id
    if _weekly_table_id:
        return _weekly_table_id

    resp = await client.get(
        f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables", headers=_h(token)
    )
    for t in resp.json().get("data", {}).get("items", []):
        if t.get("name") == "CEO周报":
            _weekly_table_id = t["table_id"]
            return _weekly_table_id

    # Create table
    resp = await client.post(
        f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables",
        headers=_h(token),
        json={"table": {"name": "CEO周报", "fields": [
            {"field_name": "周期",    "type": 1},
            {"field_name": "日期范围", "type": 1},
            {"field_name": "周报全文", "type": 1},
            {"field_name": "资金摘要", "type": 1},
            {"field_name": "下周重点", "type": 1},
        ]}},
    )
    _weekly_table_id = resp.json()["data"]["table_id"]
    logger.info("Created CEO周报 table: %s", _weekly_table_id)
    return _weekly_table_id


def _extract_section(report: str, keyword: str) -> str:
    lines = report.splitlines()
    capturing, result = False, []
    for line in lines:
        if line.strip().startswith("## ") and keyword in line:
            capturing = True
            continue
        if capturing:
            if line.strip().startswith("## "):
                break
            result.append(line)
    return "\n".join(result).strip()


async def _upsert_weekly_record(
    client: httpx.AsyncClient, token: str, table_id: str,
    week_key: str, date_range: str, full_report: str,
    receipts: list[dict], payments: list[dict],
    shipments: list[dict],
) -> str | None:
    total_receipt = sum(r["amount"] for r in receipts)
    total_payment = sum(p["amount"] for p in payments)
    total_ship = sum(s["amount"] for s in shipments)
    finance_summary = (
        f"收款: {total_receipt:,.0f}元（{len(receipts)}笔）| "
        f"付款: {total_payment:,.0f}元（{len(payments)}笔）| "
        f"发货货款: {total_ship:,.0f}元（{len(shipments)}票）"
    )
    next_week = _extract_section(full_report, "下周")

    fields = {
        "周期": week_key,
        "日期范围": date_range,
        "周报全文": full_report,
        "资金摘要": finance_summary,
        "下周重点": next_week,
    }

    # Search for existing record
    search = await client.post(
        f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{table_id}/records/search",
        headers=_h(token),
        json={"filter": {"conjunction": "and", "conditions": [
            {"field_name": "周期", "operator": "is", "value": [week_key]}
        ]}},
    )
    items = search.json().get("data", {}).get("items", [])

    if items:
        record_id = items[0]["record_id"]
        await client.put(
            f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{table_id}/records/{record_id}",
            headers=_h(token), json={"fields": fields},
        )
        logger.info("CEO周报 updated: %s", week_key)
        return record_id
    else:
        resp = await client.post(
            f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{table_id}/records",
            headers=_h(token), json={"fields": fields},
        )
        data = resp.json()
        if data.get("code") == 0:
            record_id = data["data"]["record"]["record_id"]
            logger.info("CEO周报 created: %s", week_key)
            return record_id
        logger.error("CEO周报 write failed: %s", data.get("msg"))
        return None


async def _send_webhook(week_label: str, report: str, receipts: list, payments: list, shipments: list) -> None:
    if not settings.feishu_webhook_url:
        return
    total_r = sum(r["amount"] for r in receipts)
    total_p = sum(p["amount"] for p in payments)
    total_s = sum(s["amount"] for s in shipments)
    next_week = _extract_section(report, "下周")

    lines = [
        [{"tag": "text", "text": f"📊 本周数据：跟进{len(receipts)}客户 | 收款{total_r:,.0f}元 | 付款{total_p:,.0f}元 | 发货{len(shipments)}票({total_s:,.0f}元)\n"}],
    ]
    if next_week:
        for l in next_week.splitlines()[:5]:
            if l.strip():
                lines.append([{"tag": "text", "text": l}])
    lines.append([{"tag": "text", "text": "\n➡️ 完整周报见飞书 CEO周报 表"}])

    payload = {"msg_type": "post", "content": {"post": {"zh_cn": {
        "title": f"📋 CEO周报 — {week_label}",
        "content": lines,
    }}}}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(settings.feishu_webhook_url, json=payload)
    except Exception as e:
        logger.error("Weekly report webhook error: %s", e)


# ── Main entry point ───────────────────────────────────────────────────

async def run_weekly_ceo_report(days: int = 7) -> bool:
    """Fetch all data sources, generate AI weekly report, write to Feishu CEO周报."""
    now = datetime.now(CST)
    week_start = now - timedelta(days=days - 1)
    cutoff_ts = week_start.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_key = now.strftime("%Y-W%V")
    date_range = f"{week_start.strftime('%m/%d')}–{now.strftime('%m/%d')}"
    week_label = f"{week_start.strftime('%m/%d')} 至 {now.strftime('%m/%d')}"

    logger.info("Generating CEO weekly report: %s", week_label)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _get_token(client)
            if not token:
                logger.error("Feishu token failed")
                return False

            # Fetch all data sources in parallel
            import asyncio
            daily_reports, followups, contracts, receipts, payments, shipments = await asyncio.gather(
                _fetch_daily_reports(client, token, cutoff_ts),
                _fetch_followups(client, token, cutoff_ts),
                _fetch_contracts(client, token, cutoff_ts),
                _fetch_receipts(client, token, cutoff_ts),
                _fetch_payments(client, token, cutoff_ts),
                _fetch_shipments(client, token, cutoff_ts),
            )

            logger.info(
                "Fetched: %d daily reports, %d followups, %d contracts, "
                "%d receipts, %d payments, %d shipments",
                len(daily_reports), len(followups), len(contracts),
                len(receipts), len(payments), len(shipments),
            )

            # Build context and generate AI summary
            context = _build_context(
                daily_reports, followups, contracts, receipts, payments, shipments, week_label
            )
            logger.info("Generating AI summary (%d chars context)...", len(context))
            full_report = await _generate_summary(context, week_label)

            # Write to Feishu
            table_id = await _ensure_weekly_table(client, token)
            record_id = await _upsert_weekly_record(
                client, token, table_id,
                week_key, date_range, full_report,
                receipts, payments, shipments,
            )

            if record_id:
                await _send_webhook(week_label, full_report, receipts, payments, shipments)
                logger.info("CEO weekly report done: %s → %s", week_key, record_id)
                return True

    except Exception:
        logger.exception("CEO weekly report failed")

    return False
