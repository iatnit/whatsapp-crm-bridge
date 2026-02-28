"""Generate a daily summary report from pipeline results."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def _get_unmatched_list() -> list[dict]:
    """Fetch unmatched conversations for the report."""
    from app.store.conversations import get_unmatched_conversations
    return await get_unmatched_conversations()


def generate_daily_report(summary: dict, unmatched: list[dict] | None = None) -> str:
    """Format the pipeline summary into a readable daily report.

    Returns a Markdown string suitable for logging, Feishu, or other output.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total_conv = summary.get("total_conversations", 0)
    analyzed = summary.get("analyzed", 0)
    written = summary.get("written", 0)
    errors = summary.get("errors", [])
    new_matches = summary.get("new_matches", 0)
    total_msgs = summary.get("total_messages", 0)

    lines = [
        f"# WhatsApp CRM 日报 — {now}",
        "",
        "## 概览",
        f"- 当日对话数: {total_conv}",
        f"- 总消息量: {total_msgs}",
        f"- 已分析: {analyzed}",
        f"- 已写入飞书: {written}",
        f"- 写入失败: {analyzed - written}",
        f"- 新匹配客户: {new_matches}",
        "",
    ]

    # Per-customer details
    results = summary.get("results", [])
    if results:
        lines.append("## 客户对话详情")
        lines.append("")
        for r in results:
            analysis = r.get("analysis", {})
            name = r.get("customer_name", "Unknown")
            phone = r.get("phone", "")
            status_icon = "OK" if r.get("feishu_written") else "FAIL"

            lines.append(f"### {name} ({phone}) [{status_icon}]")
            lines.append(f"- **摘要**: {analysis.get('summary', 'N/A')}")
            lines.append(f"- **需求**: {analysis.get('demand_summary', 'N/A')}")

            next_actions = analysis.get("next_actions", {})
            if next_actions:
                lines.append(f"- **今天**: {next_actions.get('today', '-')}")
                lines.append(f"- **明天**: {next_actions.get('tomorrow', '-')}")
                lines.append(f"- **等客户**: {next_actions.get('pending_customer', '-')}")

            codes = analysis.get("recommended_codes", [])
            if codes:
                lines.append(f"- **推荐编码**: {', '.join(codes)}")
            lines.append("")

    # Feishu write stats
    if results:
        success = sum(1 for r in results if r.get("feishu_written"))
        fail = len(results) - success
        lines.append("## 飞书写入统计")
        lines.append(f"- 成功: {success}")
        lines.append(f"- 失败: {fail}")
        lines.append("")

    # HubSpot write stats
    if results:
        hs_ok = sum(1 for r in results if r.get("hubspot_written"))
        hs_fail = len(results) - hs_ok
        lines.append("## HubSpot写入统计")
        lines.append(f"- 成功: {hs_ok}")
        lines.append(f"- 失败: {hs_fail}")
        lines.append("")

    # Unmatched customers
    if unmatched:
        lines.append("## 未匹配客户（需人工确认）")
        lines.append("")
        for conv in unmatched:
            phone = conv.get("phone", "")
            display = conv.get("display_name", "") or "Unknown"
            msgs = conv.get("total_messages", 0)
            lines.append(f"- **{display}** ({phone}) — {msgs} 条消息")
        lines.append("")

    # Errors
    if errors:
        lines.append("## 异常")
        for err in errors:
            lines.append(f"- {err}")
        lines.append("")

    lines.append("---")
    lines.append(f"*Report generated at {now}*")

    report = "\n".join(lines)
    logger.info("Daily report generated (%d chars)", len(report))
    return report
