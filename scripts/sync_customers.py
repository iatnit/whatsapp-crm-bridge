#!/usr/bin/env python3
"""Sync CRM customer data from the main vault to the bridge's local copy.

Usage (from local machine):
    python scripts/sync_customers.py /path/to/Data/crm_customers.json

Or via scp to copy onto the server:
    scp Data/crm_customers.json server:/app/data/crm_customers.json
"""

import json
import shutil
import sys
from pathlib import Path


def sync(source: str, dest: str = "data/crm_customers.json"):
    src = Path(source)
    dst = Path(dest)

    if not src.exists():
        print(f"[ERROR] Source not found: {src}")
        sys.exit(1)

    # Validate JSON
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    print(f"[OK] Loaded {len(data)} customers from {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[OK] Copied to {dst}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/sync_customers.py <source_json_path>")
        sys.exit(1)
    sync(sys.argv[1])
