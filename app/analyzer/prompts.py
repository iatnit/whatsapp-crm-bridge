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
  "is_new_customer": true 或 false,
  "crm_fields": {{
    "customer_type": "manufacturer / wholesaler / retailer / agent / brand / unknown",
    "industry": ["garment", "shoes", "bags", "accessories", "crafts", "bridal", "home_textile"],
    "competitor_mentioned": ["amy", "coco", "yang", "preciosa", "其他名称"],
    "moq_qualified": true 或 false 或 null,
    "price_sensitivity": "high / medium / low / unknown"
  }},
  "order_info": {{
    "order_confirmed": false,
    "order_products": [],
    "order_description": ""
  }}
}}

规则：
- 所有 followup_title、followup_detail、summary 必须用中文
- 规格不完整时在 demand_summary 中标注
- followup_title 简洁明了，如"询价DR14-6mm"、"确认收到样品"
- followup_detail 要包含足够细节，方便 CRM 查看
- summary 一句话概括，方便快速浏览
- recommended_codes 用 LOCA 编码格式：DR14-6mm、DS40-24x40 等
- crm_fields.customer_type: 根据对话推断客户类型。提到工厂/生产→manufacturer，批发→wholesaler，零售→retailer，代理→agent，品牌→brand，不确定→unknown
- crm_fields.industry: 根据产品用途推断行业，只返回匹配的，未提到则返回空数组
- crm_fields.competitor_mentioned: 如提到竞品供应商（amy/coco/yang/preciosa等），列出；未提到返回空数组
- crm_fields.moq_qualified: 如客户需求量达到50000米或50箱以上→true，明确低于→false，未提到→null
- crm_fields.price_sensitivity: 频繁砍价→high，讨论但可接受→medium，不太关注价格→low，未涉及→unknown
- order_info.order_confirmed: 客户明确下单/确认订单/说了"I want to order"/"please arrange"/"confirmed"等→true，仅询价或讨论→false
- order_info.order_products: 如果确认下单，列出确认的产品编码（如 ["DR14-6mm", "DS40-24x40"]），未下单则留空数组
- order_info.order_description: 如果确认下单，简述订单内容（如 "DR14-6mm 红色 100米 + DS40 透明 50张"），未下单则留空
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
