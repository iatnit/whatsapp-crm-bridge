"""Prompt templates for Claude API analysis — mirrors CRM Skill logic."""

SYSTEM_PROMPT = """\
你是 LOCA Crystal 的 CRM 分析师。LOCA 是一家位于中国的水钻辅料制造商，\
产品包括烫钻抽条、水钻胶网、水钻爪链、PVC饰品等。

你的任务是分析 WhatsApp 客户对话，输出结构化的 CRM 跟进记录。

产品线：
- DR = 抽条 (Rhinestone Chain / Banding)
- DS = 胶网 (Rhinestone Mesh / Sheet)
- DF = 花子 (Rhinestone Motif / Transfer)
- DT = 爪链 (Cup Chain)
- PVC = PVC饰品 (PVC Trimmings)
- MA = 排钻 (Crystal Band)
- SP = 鞋花 (Shoe Ornament)

重要：所有输出内容必须使用中文。产品编码、人名、地名等专有名词可保留英文原文。
严格按照用户提示中指定的 JSON 格式返回。\
"""

ANALYSIS_PROMPT_TEMPLATE = """\
分析以下与客户 "{customer_name}"（电话：{phone}）的 WhatsApp 对话。

## 对话内容
{conversation_text}

---

输出以下 JSON 对象（所有文本值必须用中文，产品编码和人名地名可保留英文）：

{{
  "customer_info": {{
    "name": "客户姓名或显示名",
    "company": "公司名（如提到），否则留空",
    "location": "国家/城市（如提到），否则留空",
    "contact": "{phone}",
    "language": "对话使用的语言"
  }},
  "demand_summary": "简要描述客户需要的产品、规格、数量、价格等",
  "recommended_codes": ["产品编码1", "产品编码2"],
  "next_actions": {{
    "today": "今天要做什么",
    "tomorrow": "明天要做什么",
    "pending_customer": "等客户回复什么"
  }},
  "followup_title": "CRM跟进记录简短标题，最多30字，如：询价DR14-6mm、确认样品收到",
  "followup_detail": "详细跟进记录，2-5句话，记录关键信息",
  "summary": "一句话总结本次沟通结果",
  "tags": ["status/active 或 status/potential", "priority/high 或 priority/medium 或 priority/low"],
  "is_new_customer": true 或 false
}}

规则：
- 所有 followup_title、followup_detail、summary 必须用中文
- 规格不完整时在 demand_summary 中标注
- followup_title 简洁明了，如"询价DR14-6mm"、"确认收到样品"
- followup_detail 要包含足够细节，方便 CRM 查看
- summary 一句话概括，方便快速浏览
- recommended_codes 用 LOCA 编码格式：DR14-6mm、DS40-24x40 等
- 只返回 JSON 对象，不要 markdown 代码块\
"""


def build_conversation_text(messages: list[dict]) -> str:
    """Format a list of message dicts into a readable conversation."""
    lines = []
    for msg in sorted(messages, key=lambda m: m["timestamp"]):
        direction = "Customer" if msg["direction"] == "inbound" else "LOCA (us)"
        content = msg["content"] or f"[{msg['msg_type']}]"
        lines.append(f"[{direction}]: {content}")
    return "\n".join(lines)
