"""Feishu Bot Agent — Gemini function-calling with CRM tools."""

import json
import logging
import time
from datetime import datetime, timezone, timedelta

from app.llm.gemini import call_gemini_with_tools

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# ── Tool declarations (Gemini format) ────────────────────────────────

TOOL_DECLARATIONS = [
    {
        "function_declarations": [
            {
                "name": "run_daily_pipeline",
                "description": "触发 CRM 日分析 Pipeline，批量分析未处理的 WhatsApp 对话并写入飞书/HubSpot",
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "send_daily_reminder",
                "description": "触发每日跟进晨报，发送到飞书群",
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "send_whatsapp_message",
                "description": "通过 WATI 发送 WhatsApp 消息给客户",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "to": {
                            "type": "STRING",
                            "description": "收件人手机号（含国家码，无+号），如 919839358409",
                        },
                        "text": {
                            "type": "STRING",
                            "description": "消息内容",
                        },
                    },
                    "required": ["to", "text"],
                },
            },
            {
                "name": "get_all_customers",
                "description": "获取所有 WhatsApp 客户对话列表（本地数据库）",
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "get_recent_messages",
                "description": "获取某客户最近的 WhatsApp 聊天记录",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "phone": {
                            "type": "STRING",
                            "description": "客户手机号（含国家码），如 919839358409",
                        },
                        "limit": {
                            "type": "INTEGER",
                            "description": "返回条数，默认 30",
                        },
                    },
                    "required": ["phone"],
                },
            },
            {
                "name": "get_customer_context",
                "description": "获取客户详细信息（名称、阶段、消息数、首次联系天数等）",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "phone": {
                            "type": "STRING",
                            "description": "客户手机号（含国家码）",
                        },
                    },
                    "required": ["phone"],
                },
            },
            {
                "name": "get_sync_status",
                "description": "查看 CRM 数据同步健康状态（飞书 ↔ HubSpot 匹配情况）",
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "get_system_health",
                "description": "查看系统运行状态：uptime、Pipeline 状态、各服务开关",
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "set_ai_status",
                "description": "开启或关闭某客户的 AI 自动回复",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "phone": {
                            "type": "STRING",
                            "description": "客户手机号",
                        },
                        "disabled": {
                            "type": "BOOLEAN",
                            "description": "true=关闭AI回复, false=开启AI回复",
                        },
                    },
                    "required": ["phone", "disabled"],
                },
            },
            {
                "name": "get_pending_actions",
                "description": "获取今日（或指定日期）的客户待办行动",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "date": {
                            "type": "STRING",
                            "description": "日期 YYYY-MM-DD，不传则默认今天（CST）",
                        },
                    },
                },
            },
        ]
    }
]

SYSTEM_PROMPT = """\
你是 LOCA Crystal 的 CRM 助手，通过飞书机器人与老板对话。
你可以调用工具来操作 CRM 系统（触发 Pipeline、发 WhatsApp 消息、查客户信息等）。

规则：
1. 用中文回复，简洁明了
2. 如果用户意图明确，直接调用对应工具，不要反复确认
3. 发 WhatsApp 消息前，确认收件人号码和内容
4. 工具执行结果较长时，做摘要后回复
5. 如果不需要调用工具，直接文本回复即可
"""


# ── Tool dispatch ────────────────────────────────────────────────────

async def _dispatch(function_name: str, arguments: dict) -> str:
    """Execute a tool and return the result as a string."""
    try:
        if function_name == "run_daily_pipeline":
            from app.analyzer.daily_pipeline import run_daily_pipeline
            summary = await run_daily_pipeline()
            return json.dumps(summary, ensure_ascii=False, default=str)

        elif function_name == "send_daily_reminder":
            from app.notifier.daily_reminder import send_daily_reminder
            ok = await send_daily_reminder()
            return "晨报已发送 ✓" if ok else "晨报发送失败"

        elif function_name == "send_whatsapp_message":
            from app.webhook.sender import send_text_message
            to = arguments.get("to", "")
            text = arguments.get("text", "")
            if not to or not text:
                return "错误：缺少 to 或 text 参数"
            wa_id = await send_text_message(to, text)
            if wa_id:
                return f"已发送到 {to}，消息ID: {wa_id}"
            return f"发送到 {to} 失败"

        elif function_name == "get_all_customers":
            from app.store.conversations import get_all_conversations
            convs = await get_all_conversations()
            lines = [f"共 {len(convs)} 个客户对话："]
            for c in convs[:50]:
                name = c.get("customer_name") or c.get("display_name") or c["phone"]
                msgs = c.get("total_messages", 0)
                lines.append(f"  • {name} ({c['phone']}) - {msgs}条消息")
            if len(convs) > 50:
                lines.append(f"  ...还有 {len(convs) - 50} 个")
            return "\n".join(lines)

        elif function_name == "get_recent_messages":
            from app.store.messages import get_messages_by_phone
            phone = arguments.get("phone", "")
            limit = int(arguments.get("limit", 30))
            if not phone:
                return "错误：缺少 phone 参数"
            msgs = await get_messages_by_phone(phone, limit=limit)
            if not msgs:
                return f"没有找到 {phone} 的消息记录"
            # Reverse to chronological order
            msgs = list(reversed(msgs))
            lines = [f"{phone} 最近 {len(msgs)} 条消息："]
            for m in msgs:
                direction = "→" if m.get("direction") == "outbound" else "←"
                ts = m.get("timestamp", 0)
                t = datetime.fromtimestamp(ts, tz=CST).strftime("%m-%d %H:%M") if ts else "?"
                content = (m.get("content") or "")[:100]
                lines.append(f"  {t} {direction} {content}")
            return "\n".join(lines)

        elif function_name == "get_customer_context":
            from app.store.conversations import get_customer_context
            phone = arguments.get("phone", "")
            if not phone:
                return "错误：缺少 phone 参数"
            ctx = await get_customer_context(phone)
            return json.dumps(ctx, ensure_ascii=False, default=str)

        elif function_name == "get_sync_status":
            from app.store.conversations import get_sync_status
            status = await get_sync_status()
            return json.dumps(status, ensure_ascii=False, default=str)

        elif function_name == "get_system_health":
            return _get_system_health()

        elif function_name == "set_ai_status":
            from app.store.conversations import set_ai_disabled
            phone = arguments.get("phone", "")
            disabled = arguments.get("disabled", True)
            if not phone:
                return "错误：缺少 phone 参数"
            found = await set_ai_disabled(phone, disabled=disabled)
            if not found:
                return f"未找到客户 {phone}"
            state = "已关闭" if disabled else "已开启"
            return f"{phone} 的AI自动回复{state}"

        elif function_name == "get_pending_actions":
            from app.store.conversations import get_pending_actions
            date = arguments.get("date") or datetime.now(CST).strftime("%Y-%m-%d")
            actions = await get_pending_actions(date)
            if not actions:
                return f"{date} 没有待办行动"
            lines = [f"{date} 待办（{len(actions)}项）："]
            for a in actions:
                name = a.get("customer_name", "?")
                pri = a.get("priority", "")
                action = a.get("today_action", "") or a.get("pending_customer", "")
                lines.append(f"  • [{pri}] {name}: {action}")
            return "\n".join(lines)

        else:
            return f"未知工具: {function_name}"

    except Exception as e:
        logger.exception("Tool %s failed", function_name)
        return f"工具执行出错: {e}"


def _get_system_health() -> str:
    """Build system health string from global state (inline, no async needed)."""
    from app.main import _app_start_time, _last_pipeline_at, _last_pipeline_ok
    from app.config import settings

    uptime_s = int(time.time() - _app_start_time) if _app_start_time else 0
    hours, remainder = divmod(uptime_s, 3600)
    minutes, seconds = divmod(remainder, 60)

    lines = [
        f"运行时间: {hours}h{minutes}m{seconds}s",
        f"Pipeline: 上次 {_last_pipeline_at or '未运行'}, {'成功' if _last_pipeline_ok else '失败'}",
        f"Pipeline间隔: {settings.pipeline_interval_hours}h",
        f"服务状态:",
        f"  HubSpot: {'开' if settings.hubspot_enabled else '关'}",
        f"  Obsidian同步: {'开' if settings.obsidian_sync_enabled else '关'}",
        f"  AI自动回复: {'开' if settings.auto_reply_enabled else '关'}",
        f"  LLM: {settings.llm_provider}",
    ]
    return "\n".join(lines)


# ── Main agent entry point ───────────────────────────────────────────

async def handle_message(user_text: str) -> str:
    """Process a user message: call Gemini with tools, dispatch if needed."""
    result = await call_gemini_with_tools(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_text,
        tools=TOOL_DECLARATIONS,
        temperature=0.3,
        max_tokens=2048,
    )

    if result is None:
        return "Gemini 调用失败，请稍后再试"

    if result["type"] == "text":
        return result["text"]

    # Function call
    fn_name = result["function_name"]
    fn_args = result["arguments"]
    logger.info("Feishu bot tool call: %s(%s)", fn_name, json.dumps(fn_args, ensure_ascii=False))

    tool_result = await _dispatch(fn_name, fn_args)

    # Truncate if too long
    if len(tool_result) > 4000:
        tool_result = tool_result[:3900] + "\n...(已截断)"

    return tool_result
