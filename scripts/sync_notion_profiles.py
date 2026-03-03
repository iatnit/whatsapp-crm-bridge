#!/usr/bin/env python3
"""One-time (and re-runnable) script: read Obsidian + Feishu data for all
6-digit customers and upsert their Notion customer profile pages.

Run locally (needs access to Obsidian CRM directory):
    cd whatsapp-crm-bridge
    python scripts/sync_notion_profiles.py

Optional flags:
    --dry-run    Print what would be done without calling Notion
    --limit N    Process at most N customers
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

# Allow importing from app/
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sync_notion_profiles")

# ── Obsidian CRM directory ──────────────────────────────────────────────────
# Adjust this path if your vault is in a different location
CRM_DIR = Path(
    os.environ.get(
        "OBSIDIAN_CRM_DIR",
        "/Users/zhangyun/Nutstore Files/我的坚果云/LuckyOS/LOCA-Factory-Brain/05-Sales Library/CRM",
    )
)

_6DIGIT_RE = re.compile(r"^(\d{6})[^0-9]")  # filenames starting with 6-digit ID


# ── Feishu helpers ──────────────────────────────────────────────────────────

async def _feishu_search_customer(name: str) -> dict:
    """Search Feishu customer table by name. Returns fields dict or {}."""
    if not settings.feishu_app_token or not settings.feishu_table_customers:
        return {}
    try:
        from app.writers.feishu_writer import _search_records
        items = await _search_records(
            table_id=settings.feishu_table_customers,
            field_name="客户",
            value=name,
        )
        if items:
            return items[0].get("fields", {})
    except Exception as e:
        logger.debug("Feishu search failed for %s: %s", name, e)
    return {}


async def _feishu_get_followups(name: str) -> list[dict]:
    """Get recent followup records from Feishu for this customer."""
    if not settings.feishu_app_token or not settings.feishu_table_followup:
        return []
    try:
        from app.writers.feishu_writer import _search_records
        items = await _search_records(
            table_id=settings.feishu_table_followup,
            field_name="客户名称",
            value=name,
        )
        return [it.get("fields", {}) for it in items[:5]]  # most recent 5
    except Exception as e:
        logger.debug("Feishu followup failed for %s: %s", name, e)
    return []


# ── Obsidian file discovery ─────────────────────────────────────────────────

def _find_chat_folder(customer_name: str) -> Path | None:
    """Find the chat-log folder best matching customer_name."""
    name_lower = customer_name.lower().strip()
    name_parts = re.split(r"[\s\-_]+", name_lower)

    best: Path | None = None
    best_score = 0

    for folder in CRM_DIR.iterdir():
        if not folder.is_dir():
            continue
        chat_log = folder / "chat-log.md"
        if not chat_log.exists():
            continue

        folder_lower = folder.name.lower()
        # Score: how many name parts appear in folder name
        score = sum(1 for p in name_parts if p and p in folder_lower)
        if score > best_score:
            best_score = score
            best = folder

    # Only return if at least one name part matched
    return best if best_score > 0 else None


def _read_file_safe(path: Path, max_chars: int = 8000) -> str:
    """Read a file, truncate if too long."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return text
    except Exception:
        return ""


def _collect_crm_files(folder: Path) -> list[str]:
    """Collect content of all crm-*.md files in a folder."""
    results = []
    for f in sorted(folder.glob("crm-*.md"), reverse=True)[:3]:  # latest 3
        content = _read_file_safe(f, max_chars=3000)
        if content:
            results.append(f"### {f.name}\n{content}")
    return results


# ── Gemini summarizer ───────────────────────────────────────────────────────

async def _gemini_summarize(customer_name: str, crm_file: str, chat_log: str,
                             crm_analyses: list[str], feishu_fields: dict,
                             followups: list[dict]) -> dict:
    """Call Gemini to produce a structured customer profile summary."""
    import json
    from app.llm.gemini import call_gemini

    followup_text = ""
    for f in followups:
        title = f.get("跟进标题", "")
        detail = f.get("跟进详情", "") or f.get("今日沟通详情", "")
        date = f.get("日期", "")
        if title or detail:
            followup_text += f"- [{date}] {title}: {detail}\n"

    feishu_text = ""
    for k, v in feishu_fields.items():
        if v and k not in ("id", "record_id"):
            feishu_text += f"- {k}: {v}\n"

    analyses_text = "\n\n".join(crm_analyses) if crm_analyses else "无"

    system_prompt = "你是一个专业的销售CRM分析师，擅长从多源数据中提炼客户画像。只输出JSON，不要其他文字。"

    user_prompt = f"""请根据以下客户数据，生成一份结构化的客户画像总结。

# 客户名称
{customer_name}

# 飞书CRM数据
{feishu_text or "无"}

# 飞书跟进记录
{followup_text or "无"}

# 历史CRM备注
{crm_file[:3000] if crm_file else "无"}

# WhatsApp聊天记录
{chat_log[:4000] if chat_log else "无"}

# AI自动分析历史
{analyses_text}

请输出以下JSON：
{{
  "summary": "客户总体画像（2-3句话）",
  "demand_summary": "产品需求和规格偏好",
  "location": "客户所在地区/国家",
  "products": ["感兴趣的产品线或产品编码"],
  "stage": "客户阶段（potential/contacted/sampling/ordered/regular）",
  "communication_style": "沟通风格和频率特点",
  "key_notes": "需要重点关注的事项",
  "next_actions": "建议的下一步行动"
}}"""

    text = await call_gemini(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_mode=False,
        temperature=0.3,
        max_tokens=4096,
    )
    if not text:
        return {}
    try:
        # Extract JSON from code fences or bare JSON
        clean = text.strip()
        if "```" in clean:
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        # Find first { and last }
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start:end + 1]
        return json.loads(clean)
    except Exception as e:
        logger.error("Gemini JSON parse failed for %s: %s | raw: %s", customer_name, e, repr(text[:300]))
        return {}


# ── Main processing ─────────────────────────────────────────────────────────

def _find_local_crm_file(feishu_id: str, customer_name: str) -> Path | None:
    """Find a 6-digit CRM .md file matching this customer (by ID or name)."""
    if not CRM_DIR.exists():
        return None
    # Try exact ID prefix first
    for f in CRM_DIR.iterdir():
        if f.is_file() and f.suffix == ".md" and f.name.startswith(feishu_id):
            return f
    # Fallback: name match in filename
    name_lower = customer_name.lower()
    for f in CRM_DIR.iterdir():
        if f.is_file() and f.suffix == ".md" and _6DIGIT_RE.match(f.name):
            if name_lower in f.stem.lower():
                return f
    return None


async def process_customer(customer: dict, dry_run: bool) -> bool:
    """Process one Feishu customer → Notion profile."""
    feishu_id = customer["feishu_id"]
    customer_name = customer["name"]
    phone = customer["phone"]
    location = customer["location"]

    logger.info("Processing [%s] %s (phone: %s)", feishu_id, customer_name, phone or "-")

    # 1. Read local CRM notes file (if exists)
    crm_file = _find_local_crm_file(feishu_id, customer_name)
    crm_content = _read_file_safe(crm_file) if crm_file else ""
    if crm_file:
        logger.info("  CRM file: %s", crm_file.name)

    # 2. Find chat-log folder (by name, then by phone)
    chat_folder = _find_chat_folder(customer_name)
    if not chat_folder and phone:
        # Try finding by phone number as folder name
        phone_folder = CRM_DIR / phone
        if phone_folder.exists() and (phone_folder / "chat-log.md").exists():
            chat_folder = phone_folder

    chat_log = ""
    crm_analyses: list[str] = []
    if chat_folder:
        logger.info("  Chat folder: %s", chat_folder.name)
        chat_log = _read_file_safe(chat_folder / "chat-log.md", max_chars=5000)
        crm_analyses = _collect_crm_files(chat_folder)
    else:
        logger.info("  No chat folder for '%s'", customer_name)

    # 3. Fetch Feishu followup records
    followups = await _feishu_get_followups(customer_name)
    logger.info("  Followups: %d", len(followups))

    # 4. Summarize with Gemini
    feishu_fields = {"联系电话": phone, "国家地区": location}
    analysis = await _gemini_summarize(
        customer_name, crm_content, chat_log, crm_analyses, feishu_fields, followups
    )
    if not analysis:
        logger.warning("  Skipping %s — Gemini returned nothing", customer_name)
        return False

    logger.info("  Summary: %s", analysis.get("summary", "")[:80])

    if dry_run:
        logger.info("  [DRY RUN] Would write to Notion")
        return True

    # 5. Write to Notion
    from app.writers.notion_customer_writer import upsert_customer_profile

    notion_analysis = {
        "summary": analysis.get("summary", ""),
        "demand_summary": analysis.get("demand_summary", ""),
        "recommended_codes": analysis.get("products", []),
        "tags": [f"stage/{analysis.get('stage', 'potential')}"],
        "followup_detail": (
            f"沟通风格: {analysis.get('communication_style', '')}\n"
            f"重点关注: {analysis.get('key_notes', '')}\n"
            f"建议行动: {analysis.get('next_actions', '')}"
        ),
    }

    page_id = await upsert_customer_profile(
        customer_name=customer_name,
        phone=phone,
        location=location or analysis.get("location", ""),
        analysis=notion_analysis,
        total_messages=0,
        feishu_id=feishu_id,
    )
    if page_id:
        logger.info("  Notion page: %s", page_id)
        return True
    else:
        logger.warning("  Notion write failed for %s", customer_name)
        return False


async def main():
    parser = argparse.ArgumentParser(description="Sync Feishu CRM customers (with 编号) to Notion")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Notion")
    parser.add_argument("--limit", type=int, default=0, help="Max customers to process")
    args = parser.parse_args()

    # Fetch all customers with 6-digit 编号 from Feishu
    from app.writers.feishu_writer import list_customers_with_feishu_id
    customers = await list_customers_with_feishu_id()

    if not customers:
        logger.error(
            "No customers returned from Feishu. "
            "Check FEISHU_APP_TOKEN and FEISHU_TABLE_CUSTOMERS in .env"
        )
        sys.exit(1)

    logger.info("Feishu returned %d customers with 编号", len(customers))

    if args.limit:
        customers = customers[:args.limit]

    ok = fail = 0
    for customer in customers:
        try:
            success = await process_customer(customer, dry_run=args.dry_run)
            if success:
                ok += 1
            else:
                fail += 1
        except Exception as e:
            logger.error("Unhandled error for %s: %s", customer.get("name"), e)
            fail += 1
        await asyncio.sleep(1)

    logger.info("Done: %d OK, %d failed out of %d customers", ok, fail, ok + fail)


if __name__ == "__main__":
    asyncio.run(main())
