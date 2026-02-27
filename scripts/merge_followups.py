"""Merge duplicate follow-up records (same customer + same day) in Feishu."""

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import httpx

BASE_URL = "https://open.feishu.cn/open-apis"
APP_TOKEN = "XYeCby15ga5CDKsX57YcFL1Hnce"
TABLE_ID = "tblcftbYX7E0cEUo"
APP_ID = "cli_a9f0a37109b81cc6"
APP_SECRET = "iLw0CLmMIRjc6WvMn99Bkf24bqZODqBe"


async def get_token():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"{BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
        )
        return r.json()["tenant_access_token"]


async def fetch_all(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    all_items = []
    page_token = None
    while True:
        payload = {"automatic_fields": True, "page_size": 100}
        if page_token:
            payload["page_token"] = page_token
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{BASE_URL}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/search",
                json=payload,
                headers=headers,
            )
            data = r.json()
        all_items.extend(data.get("data", {}).get("items", []))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return all_items


def ext(field):
    if isinstance(field, list):
        return " ".join(x.get("text", "") for x in field if isinstance(x, dict))
    if isinstance(field, str):
        return field
    return ""


async def update_record(token, record_id, fields):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.put(
            f"{BASE_URL}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/{record_id}",
            json={"fields": fields},
            headers=headers,
        )
        data = r.json()
    if data.get("code") != 0:
        print(f"  ✗ Update error {record_id}: {data.get('msg')}")
        return False
    return True


async def delete_record(token, record_id):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(
            f"{BASE_URL}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/{record_id}",
            headers=headers,
        )
        data = r.json()
    if data.get("code") != 0:
        print(f"  ✗ Delete error {record_id}: {data.get('msg')}")
        return False
    return True


async def main():
    token = await get_token()
    items = await fetch_all(token)
    print(f"Total records: {len(items)}")

    cst = timezone(timedelta(hours=8))
    records = []
    for item in items:
        f = item.get("fields", {})
        rid = item.get("record_id", "")
        customer_field = f.get("客户名称", {})
        customer_ids = (
            customer_field.get("link_record_ids", [])
            if isinstance(customer_field, dict)
            else []
        )
        customer_key = customer_ids[0] if customer_ids else "unknown"
        created_ts = f.get("跟进时间", 0)
        if isinstance(created_ts, (int, float)) and created_ts > 0:
            dt = datetime.fromtimestamp(created_ts / 1000, tz=cst)
            date_str = dt.strftime("%Y-%m-%d")
        else:
            date_str = "unknown"
        records.append(
            {
                "record_id": rid,
                "customer_key": customer_key,
                "date": date_str,
                "title": ext(f.get("跟进内容", "")),
                "detail": ext(f.get("跟进情况", "")),
                "summary": ext(f.get("总结", "")),
                "method": f.get("跟进形式", ""),
                "ts": created_ts,
            }
        )

    # Group by (customer, date)
    groups = defaultdict(list)
    for r in records:
        groups[(r["customer_key"], r["date"])].append(r)

    dups = {
        k: sorted(v, key=lambda x: x["ts"])
        for k, v in groups.items()
        if len(v) > 1
    }
    print(f"Duplicate groups: {len(dups)}")
    print()

    merged = 0
    deleted = 0

    for (cust, date), recs in sorted(dups.items(), key=lambda x: x[0][1]):
        keep = recs[0]
        extras = recs[1:]
        print(
            f"--- {cust[:16]} | {date} | keep {keep['record_id']} + merge {len(extras)} ---"
        )

        # Merge titles: pick the longest
        all_titles = [keep["title"]] + [
            r["title"]
            for r in extras
            if r["title"] and r["title"] != keep["title"]
        ]
        best_title = max(all_titles, key=len) if all_titles else keep["title"]

        # Merge details with separator
        all_details = [keep["detail"]] + [
            r["detail"]
            for r in extras
            if r["detail"] and r["detail"] != keep["detail"]
        ]
        merged_detail = "\n\n---\n\n".join(d for d in all_details if d)

        # Pick the longest summary
        all_summaries = [keep["summary"]] + [
            r["summary"] for r in extras if r["summary"]
        ]
        best_summary = max(all_summaries, key=len) if all_summaries else ""

        print(f"  标题: {best_title[:60]}")
        print(f"  合并详情: {len(all_details)} 段")
        print(f"  总结: {best_summary[:60]}")

        # Update kept record
        fields = {"跟进内容": best_title, "跟进情况": merged_detail}
        if best_summary:
            fields["总结"] = best_summary
        ok = await update_record(token, keep["record_id"], fields)
        if ok:
            merged += 1
            print(f"  ✓ Updated {keep['record_id']}")
        else:
            print("  ✗ Failed to update, skipping deletes")
            continue

        # Delete extras
        for extra in extras:
            ok = await delete_record(token, extra["record_id"])
            if ok:
                deleted += 1
                print(f"  ✓ Deleted {extra['record_id']}")
            else:
                print(f"  ✗ Failed to delete {extra['record_id']}")

        await asyncio.sleep(0.3)

    print(f"\n=== Done: {merged} groups merged, {deleted} records deleted ===")


if __name__ == "__main__":
    asyncio.run(main())
