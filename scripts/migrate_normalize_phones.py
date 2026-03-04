#!/usr/bin/env python3
"""One-time migration: normalize phone numbers in SQLite conversations/messages tables.

Run once on the server after deploying the router.py phone-normalization fix.

Usage:
    python scripts/migrate_normalize_phones.py
    python scripts/migrate_normalize_phones.py --dry-run
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()


def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    p = re.sub(r"[\s\-\(\)]", "", phone.strip())
    if not p.startswith("+"):
        p = "+" + p
    return p if len(re.sub(r"\D", "", p)) >= 5 else ""


def main(dry_run: bool) -> None:
    import sqlite3
    from app.config import settings

    db_path = settings.db_path if hasattr(settings, "db_path") else "data/whatsapp.db"
    print(f"Database: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Collect all phones needing normalization
    conv_rows = conn.execute("SELECT phone FROM conversations").fetchall()
    msg_phones = conn.execute("SELECT DISTINCT phone FROM messages").fetchall()

    all_phones = set(r["phone"] for r in conv_rows) | set(r["phone"] for r in msg_phones)
    to_fix = [(p, normalize_phone(p)) for p in all_phones if p and p != normalize_phone(p)]

    print(f"Total distinct phones: {len(all_phones)}")
    print(f"Non-normalized: {len(to_fix)}")

    if not to_fix:
        print("Nothing to fix.")
        conn.close()
        return

    for raw, norm in to_fix:
        print(f"  {raw!r} → {norm!r}")

    if dry_run:
        print("\n[DRY RUN — no changes made]")
        conn.close()
        return

    fixed = 0
    for raw, norm in to_fix:
        conn.execute("UPDATE conversations SET phone = ? WHERE phone = ?", (norm, raw))
        conn.execute("UPDATE messages SET phone = ? WHERE phone = ?", (norm, raw))
        fixed += 1

    conn.commit()
    conn.close()
    print(f"\nDone: {fixed} phone numbers normalized.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
