"""Generate a daily summary report from pipeline results."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def generate_daily_report(summary: dict) -> str:
    """Format the pipeline summary into a readable daily report.

    Returns a Markdown string suitable for logging, Feishu, or other output.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# WhatsApp CRM 日报 — {now}",
        "",
        "## 概览",
        f"- 当日对话数: {summary.get('total_conversations', 0)}",
        f"- 总消息量: {summary.get('total_messages', 0)}",
        f"- 已分析: {summary.get('analyzed', 0)}",
        f"- 已写入飞书: {summary.get('written', 0)}",
        f"- 新匹配客户: {summary.get('new_matches', 0)}",
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

    # Errors
    errors = summary.get("errors", [])
    if errors:
        lines.append("## 异常")
        for err in errors:
            lines.append(f"- {err}")
        lines.append("")

    # Unmatched customers
    lines.append("---")
    lines.append(f"*Report generated at {now}*")

    report = "\n".join(lines)
    logger.info("Daily report generated (%d chars)", len(report))
    return report
