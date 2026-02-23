"""Prompt templates for Claude API analysis — mirrors CRM Skill logic."""

SYSTEM_PROMPT = """\
You are a CRM analyst for LOCA Crystal, a manufacturer of rhinestone trimmings \
(hot-fix rhinestone chains, rhinestone mesh/sheets, rhinestone cup chains, \
PVC trimmings, etc.) based in China.

Your job is to analyze WhatsApp conversations between our sales team and \
customers, then produce a structured CRM follow-up record.

Product lines:
- DR = 抽条 (Rhinestone Chain / Banding)
- DS = 胶网 (Rhinestone Mesh / Sheet)
- DF = 花子 (Rhinestone Motif / Transfer)
- DT = 爪链 (Cup Chain)
- PVC = PVC饰品 (PVC Trimmings)
- MA = 排钻 (Crystal Band)
- SP = 鞋花 (Shoe Ornament)

Always respond in the exact JSON format specified in the user prompt.\
"""

ANALYSIS_PROMPT_TEMPLATE = """\
Analyze this WhatsApp conversation with customer "{customer_name}" (phone: {phone}).

## Conversation
{conversation_text}

---

Produce a JSON object with these fields (all string values):

{{
  "customer_info": {{
    "name": "customer name or display name",
    "company": "company name if mentioned, else empty",
    "location": "country/city if mentioned, else empty",
    "contact": "{phone}",
    "language": "language used in conversation"
  }},
  "demand_summary": "concise summary of products they need, specs, quantities, pricing",
  "recommended_codes": ["product code 1", "product code 2"],
  "next_actions": {{
    "today": "what we should do today",
    "tomorrow": "what we should do tomorrow",
    "pending_customer": "what we're waiting on from the customer"
  }},
  "followup_title": "short title for CRM follow-up record, max 30 chars",
  "followup_detail": "detailed follow-up notes, 2-5 sentences",
  "summary": "one-sentence summary of the conversation outcome",
  "tags": ["status/active or status/potential", "priority/high or priority/medium or priority/low"],
  "is_new_customer": true or false
}}

Rules:
- If specs are incomplete, note this in demand_summary
- followup_title should be concise like "询价DR14-6mm" or "确认样品收到"
- followup_detail should be detailed enough for CRM record
- summary is a one-liner for quick scanning
- recommended_codes use LOCA format: DR14-6mm, DS40-24x40, etc.
- Return ONLY the JSON object, no markdown fences\
"""


def build_conversation_text(messages: list[dict]) -> str:
    """Format a list of message dicts into a readable conversation."""
    lines = []
    for msg in sorted(messages, key=lambda m: m["timestamp"]):
        direction = "Customer" if msg["direction"] == "inbound" else "LOCA (us)"
        content = msg["content"] or f"[{msg['msg_type']}]"
        lines.append(f"[{direction}]: {content}")
    return "\n".join(lines)
