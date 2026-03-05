"""Merge duplicate CEO日报 records in Feishu — one record per day.

For each day with multiple records:
- Keeps the record with the longest report (most complete)
- Merges all unique customer lines from all records into one
- Deletes the rest

Usage:
    python scripts/merge_ceo_reports.py --dry-run   # preview only
    python scripts/merge_ceo_reports.py             # actually merge
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

# ── Config ────────────────────────────────────────────────────────────
FEISHU_APP_ID = "cli_a9f0a37109b81cc6"
FEISHU_APP_SECRET = "iLw0CLmMIRjc6WvMn99Bkf24bqZODqBe"
CEO_APP_TOKEN = "OPNSb3Y9la0gaAs1uN9cYAejnNd"
CEO_TABLE_ID = "tbls91RzscIQkMv4"
BASE_URL = "https://open.feishu.cn/open-apis"
CST = timezone(timedelta(hours=8))


async def get_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    return resp.json()["tenant_access_token"]


async def list_all_records(client: httpx.AsyncClient, token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    records = []
    page_token = None

    while True:
        params: dict = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token

        resp = await client.get(
            f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{CEO_TABLE_ID}/records",
            headers=headers,
            params=params,
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"Error listing records: {data.get('msg')}", file=sys.stderr)
            break

        items = data.get("data", {}).get("items", [])
        records.extend(items)

        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token")

    return records


async def update_record(client: httpx.AsyncClient, token: str, record_id: str, fields: dict) -> bool:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = await client.put(
        f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{CEO_TABLE_ID}/records/{record_id}",
        headers=headers,
        json={"fields": fields},
    )
    data = resp.json()
    return data.get("code") == 0


async def batch_delete(client: httpx.AsyncClient, token: str, record_ids: list[str]) -> int:
    """Delete records in batches of 500. Returns number deleted."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    deleted = 0
    for i in range(0, len(record_ids), 500):
        batch = record_ids[i : i + 500]
        resp = await client.post(
            f"{BASE_URL}/bitable/v1/apps/{CEO_APP_TOKEN}/tables/{CEO_TABLE_ID}/records/batch_delete",
            headers=headers,
            json={"records": batch},
        )
        if resp.json().get("code") == 0:
            deleted += len(batch)
    return deleted


def record_date(r: dict) -> str:
    """Extract date string (YYYY-MM-DD CST) from a record's 日期 field."""
    val = r.get("fields", {}).get("日期")
    if val is None:
        return "unknown"
    # Feishu returns date field as int (ms) or dict with "value"
    if isinstance(val, dict):
        ts_ms = val.get("value") or 0
    elif isinstance(val, (int, float)):
        ts_ms = int(val)
    else:
        ts_ms = 0
    if not ts_ms:
        return "unknown"
    return datetime.fromtimestamp(ts_ms / 1000, tz=CST).strftime("%Y-%m-%d")


async def main(dry_run: bool) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        print("Authenticating with Feishu...")
        token = await get_token(client)

        print("Fetching all CEO日报 records...")
        records = await list_all_records(client, token)
        print(f"Total records: {len(records)}")

        # Group by date
        by_date: dict[str, list[dict]] = defaultdict(list)
        for r in records:
            by_date[record_date(r)].append(r)

        duplicates = {d: recs for d, recs in by_date.items() if len(recs) > 1}
        print(f"\nDates with duplicates: {len(duplicates)}")
        for d in sorted(duplicates):
            print(f"  {d}: {len(duplicates[d])} records")

        if not duplicates:
            print("\nNo duplicates found. Nothing to do.")
            return

        if dry_run:
            print("\n[DRY RUN] No changes made. Run without --dry-run to apply.")
            return

        print("\nMerging...")
        total_deleted = 0

        for date_str, recs in sorted(duplicates.items()):
            # Pick the record with the longest report as the keeper
            keep = max(recs, key=lambda r: len(r.get("fields", {}).get("今日日报全文") or ""))

            # Merge all unique customer lines from every record
            customer_lines: set[str] = set()
            for r in recs:
                raw = r.get("fields", {}).get("客户相关") or ""
                for line in raw.splitlines():
                    line = line.strip()
                    if line and line != "无新客户对话":
                        customer_lines.add(line)

            merged_customers = "\n".join(sorted(customer_lines)) or "无新客户对话"
            keep_id = keep["record_id"]

            # Update keeper with merged customer content
            ok = await update_record(client, token, keep_id, {
                "今日日报全文": keep["fields"].get("今日日报全文") or "",
                "客户相关": merged_customers,
            })
            if not ok:
                print(f"  {date_str}: ⚠️  update failed, skipping")
                continue

            # Delete all other records for this date
            to_delete = [r["record_id"] for r in recs if r["record_id"] != keep_id]
            deleted = await batch_delete(client, token, to_delete)
            total_deleted += deleted
            print(f"  {date_str}: kept {keep_id}, deleted {deleted} duplicate(s), merged {len(customer_lines)} customer entries")

        print(f"\n✅ Done. {total_deleted} duplicate records deleted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge duplicate Feishu CEO日报 records")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
