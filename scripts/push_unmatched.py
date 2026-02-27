"""Push unmatched WhatsApp customers to Feishu '未匹配客户表'.

Usage (inside Docker):
    python3 scripts/push_unmatched.py

Usage (standalone):
    pip install httpx
    python3 scripts/push_unmatched.py
"""

import sqlite3
import sys

import httpx

# Feishu config
APP_ID = "cli_a9f0a37109b81cc6"
APP_SECRET = "iLw0CLmMIRjc6WvMn99Bkf24bqZODqBe"
APP_TOKEN = "XYeCby15ga5CDKsX57YcFL1Hnce"
BASE = "https://open.feishu.cn/open-apis"
DB_PATH = "data/whatsapp.db"


def get_token():
    r = httpx.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=15,
    )
    data = r.json()
    if data.get("code") != 0:
        print(f"Token error: {data}")
        sys.exit(1)
    print("Token OK")
    return data["tenant_access_token"]


def find_table(token):
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get(
        f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables",
        headers=headers,
        timeout=15,
    )
    tables = r.json().get("data", {}).get("items", [])
    table_id = None
    for t in tables:
        name = t["name"]
        tid = t["table_id"]
        print(f"  {tid} | {name}")
        if "未匹配" in name:
            table_id = tid
    if not table_id:
        print("ERROR: 未找到 '未匹配客户表', 请先在飞书多维表格中创建")
        sys.exit(1)
    print(f"Target table: {table_id}")
    return table_id


def get_unmatched():
    db = sqlite3.connect(DB_PATH)
    rows = db.execute(
        "SELECT phone, display_name, message_count "
        "FROM conversations "
        "WHERE match_status != 'matched' "
        "ORDER BY message_count DESC"
    ).fetchall()
    db.close()
    print(f"Unmatched customers: {len(rows)}")
    return rows


def push_to_feishu(token, table_id, rows):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    ok = 0
    fail = 0
    for phone, name, count in rows:
        fields = {
            "客户名称": name or phone,
            "电话号码": str(phone),
            "消息数量": count or 0,
        }
        r = httpx.post(
            f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records",
            json={"fields": fields},
            headers=headers,
            timeout=15,
        )
        if r.json().get("code") == 0:
            ok += 1
            print(f"  OK: {name} ({phone})")
        else:
            fail += 1
            msg = r.json().get("msg", "unknown")
            print(f"  FAIL: {name} ({phone}) - {msg}")
    print(f"\nDone: {ok} success, {fail} failed, {len(rows)} total")


def main():
    print("=== Push Unmatched Customers to Feishu ===\n")
    token = get_token()
    table_id = find_table(token)
    rows = get_unmatched()
    if not rows:
        print("No unmatched customers found!")
        return
    push_to_feishu(token, table_id, rows)


if __name__ == "__main__":
    main()
