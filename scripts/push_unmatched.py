"""Push unmatched WhatsApp customers to Feishu '未匹配客户表'.

Usage (via docker run):
    cd /opt/whatsapp-crm-bridge
    docker run --rm -v ./data:/app/data -v ./scripts:/app/scripts -w /app \
        whatsapp-crm-bridge-whatsapp-crm python3 scripts/push_unmatched.py
"""

import sqlite3
import sys
from datetime import datetime

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
    table_id = None
    page_token = None

    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = httpx.get(
            f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables",
            headers=headers,
            params=params,
            timeout=15,
        )
        data = r.json().get("data", {})
        tables = data.get("items", [])
        for t in tables:
            name = t["name"]
            tid = t["table_id"]
            print(f"  {tid} | {name}")
            if "未匹配" in name:
                table_id = tid

        if not data.get("has_more"):
            break
        page_token = data.get("page_token")

    if not table_id:
        print("ERROR: 未找到 '未匹配客户表', 请先在飞书多维表格中创建")
        print("Tip: 确保飞书应用有该表的访问权限")
        sys.exit(1)
    print(f"Target table: {table_id}")
    return table_id


def get_fields(token, table_id):
    """List fields in the target table to help debug column name issues."""
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get(
        f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields",
        headers=headers,
        timeout=15,
    )
    fields = r.json().get("data", {}).get("items", [])
    print(f"\nTable fields:")
    for f in fields:
        print(f"  {f['field_name']} (type={f['type']})")
    return [f["field_name"] for f in fields]


def get_unmatched():
    db = sqlite3.connect(DB_PATH)
    rows = db.execute(
        "SELECT phone, display_name, message_count "
        "FROM conversations "
        "WHERE match_status != 'matched' "
        "ORDER BY message_count DESC"
    ).fetchall()
    db.close()
    print(f"\nUnmatched customers: {len(rows)}")
    return rows


def push_to_feishu(token, table_id, rows, field_names):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    today = datetime.now().strftime("%Y-%m-%d")
    ok = 0
    fail = 0

    for phone, name, count in rows:
        # Try to match field names flexibly
        fields = {}
        for fn in field_names:
            fn_lower = fn.lower()
            if "客户" in fn or "名称" in fn or "name" in fn_lower:
                fields[fn] = name or phone
            elif "电话" in fn or "phone" in fn_lower:
                fields[fn] = str(phone)
            elif "日期" in fn or "date" in fn_lower:
                fields[fn] = today
            elif "消息" in fn or "数量" in fn or "count" in fn_lower:
                fields[fn] = count or 0

        if not fields:
            fields = {"新客户名称": name or phone, "电话": str(phone), "日期": today}

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
    field_names = get_fields(token, table_id)
    rows = get_unmatched()
    if not rows:
        print("No unmatched customers found!")
        return
    push_to_feishu(token, table_id, rows, field_names)


if __name__ == "__main__":
    main()
