"""Batch translate English follow-up records in Feishu to Chinese.

Usage: python scripts/translate_followups.py [--dry-run]
"""

import asyncio
import json
import os
import re
import sys
import time

import httpx

# ── Config ──────────────────────────────────────────────────────────

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "cli_a9f0a37109b81cc6")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "iLw0CLmMIRjc6WvMn99Bkf24bqZODqBe")
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "XYeCby15ga5CDKsX57YcFL1Hnce")
FEISHU_TABLE_FOLLOWUP = "tblcftbYX7E0cEUo"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
BASE_URL = "https://open.feishu.cn/open-apis"

DRY_RUN = "--dry-run" in sys.argv


# ── Feishu helpers ──────────────────────────────────────────────────

async def get_feishu_token() -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Token error: {data}")
        return data["tenant_access_token"]


async def fetch_all_records(token: str) -> list[dict]:
    """Fetch all follow-up records with pagination."""
    all_items = []
    page_token = None

    while True:
        payload = {"automatic_fields": True, "page_size": 200}
        if page_token:
            payload["page_token"] = page_token

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{BASE_URL}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_FOLLOWUP}/records/search",
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            data = resp.json()

        if data.get("code") != 0:
            print(f"Search error: {data.get('msg')}")
            break

        items = data.get("data", {}).get("items", [])
        all_items.extend(items)

        if not data.get("data", {}).get("has_more"):
            break
        page_token = data["data"].get("page_token")

    return all_items


async def update_record(token: str, record_id: str, fields: dict) -> bool:
    """Update a Feishu record."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{BASE_URL}/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_FOLLOWUP}/records/{record_id}",
            json={"fields": fields},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        data = resp.json()
    if data.get("code") != 0:
        print(f"  ✗ Update error for {record_id}: {data.get('msg')}")
        return False
    return True


# ── Text helpers ────────────────────────────────────────────────────

def extract_text(field) -> str:
    """Extract plain text from Feishu rich text field."""
    if isinstance(field, list):
        return " ".join(x.get("text", "") for x in field if isinstance(x, dict))
    if isinstance(field, str):
        return field
    return ""


def has_substantial_english(text: str) -> bool:
    """Check if text has substantial English (not just product codes/names)."""
    # Common English words that indicate auto-generated English content
    english_indicators = [
        "customer", "initiated", "contact", "inquiry", "inquired",
        "product", "requested", "mentioned", "details", "required",
        "sales team", "needs to", "further", "specific", "information",
        "regarding", "provided", "currently", "however", "established",
        "communication", "proceed", "requirements", "specifications",
        "clarification", "engagement", "promptly", "understand",
    ]
    text_lower = text.lower()
    matches = sum(1 for w in english_indicators if w in text_lower)
    return matches >= 3


# ── Gemini translation ──────────────────────────────────────────────

TRANSLATE_PROMPT = """\
你是 LOCA Crystal 的 CRM 助手。请将以下 CRM 跟进记录翻译成中文。

要求：
1. 所有内容翻译成中文
2. 产品编码（如 DR-14-6mm、SS4、4Lines）、人名、地名保留英文
3. 保持原有的商业信息准确性
4. 语言简洁专业，适合 CRM 系统阅读
5. 如果原文是很泛泛的初次联系记录（如 "Customer X initiated contact"），简化为一句话即可

输入：
跟进内容：{title}
跟进情况：{detail}
总结：{summary}

输出 JSON 格式（只返回 JSON，不要 markdown）：
{{"title": "中文标题", "detail": "中文跟进情况", "summary": "中文总结"}}
"""


async def translate_with_gemini(title: str, detail: str, summary: str) -> dict | None:
    """Translate a record to Chinese using Gemini."""
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY not set")
        return None

    # Truncate very long fields to prevent token overflow
    if len(detail) > 800:
        detail = detail[:800] + "..."
    if len(summary) > 300:
        summary = summary[:300] + "..."

    # Escape special chars that might break the prompt
    title_safe = title.replace('"', '\\"').replace('\n', ' ')
    detail_safe = detail.replace('"', '\\"').replace('\n', '\\n')
    summary_safe = summary.replace('"', '\\"').replace('\n', ' ')

    prompt = TRANSLATE_PROMPT.format(
        title=title_safe, detail=detail_safe, summary=summary_safe
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            print(f"  Gemini error {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            print(f"  Gemini: no candidates returned")
            return None

        text = candidates[0]["content"]["parts"][0]["text"]
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]

        return json.loads(text.strip())

    except json.JSONDecodeError:
        # Try to fix common JSON issues
        try:
            # Sometimes Gemini adds trailing content after the JSON
            text = text.strip()
            # Find the last }
            last_brace = text.rfind("}")
            if last_brace > 0:
                return json.loads(text[: last_brace + 1])
        except Exception:
            pass
        print(f"  Gemini: JSON parse error for response: {text[:100]}...")
        return None
    except Exception as e:
        print(f"  Gemini error: {e}")
        return None


# ── Main ────────────────────────────────────────────────────────────

async def main():
    print("=== Feishu Follow-up Records: English → Chinese Translation ===\n")

    if DRY_RUN:
        print("🔍 DRY RUN mode — no changes will be made\n")

    token = await get_feishu_token()
    records = await fetch_all_records(token)
    print(f"Total records: {len(records)}\n")

    # Find English records
    english_records = []
    for item in records:
        f = item.get("fields", {})
        title = extract_text(f.get("跟进内容", ""))
        detail = extract_text(f.get("跟进情况", ""))
        summary = extract_text(f.get("总结", ""))

        all_text = f"{title} {detail} {summary}"
        if has_substantial_english(all_text):
            english_records.append({
                "record_id": item["record_id"],
                "title": title,
                "detail": detail,
                "summary": summary,
            })

    print(f"English records to translate: {len(english_records)}\n")

    if not english_records:
        print("No English records found. Done!")
        return

    translated = 0
    failed = 0

    for i, rec in enumerate(english_records):
        rid = rec["record_id"]
        print(f"[{i+1}/{len(english_records)}] {rid}: {rec['title'][:50]}...")

        # Translate
        result = await translate_with_gemini(rec["title"], rec["detail"], rec["summary"])
        if not result:
            print("  ✗ Translation failed, skipping")
            failed += 1
            continue

        new_title = result.get("title", rec["title"])
        new_detail = result.get("detail", rec["detail"])
        new_summary = result.get("summary", rec["summary"])

        print(f"  → 标题: {new_title[:50]}")
        print(f"  → 总结: {new_summary[:60]}")

        if DRY_RUN:
            print("  [DRY RUN] Would update")
        else:
            fields = {"跟进内容": new_title, "跟进情况": new_detail}
            if new_summary:
                fields["总结"] = new_summary
            ok = await update_record(token, rid, fields)
            if ok:
                print("  ✓ Updated")
                translated += 1
            else:
                failed += 1

        # Rate limit: ~2 requests/sec for Gemini
        await asyncio.sleep(0.5)

    print(f"\n=== Done: {translated} translated, {failed} failed ===")


if __name__ == "__main__":
    asyncio.run(main())
