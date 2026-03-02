#!/usr/bin/env python3
"""Remove <!-- message_id --> comment lines from chat-log.md files.
Extract IDs to seen_ids.txt for idempotency tracking.

Run from project root:
    python scripts/clean_chat_log_comments.py [--dry-run]
"""

import re
import sys
from pathlib import Path

CRM_BASE = (
    Path.home()
    / "Nutstore Files/我的坚果云/LuckyOS/LOCA-Factory-Brain/05-Sales Library/CRM"
)
DRY_RUN = "--dry-run" in sys.argv

COMMENT_RE = re.compile(r"^<!-- (.+) -->$")


def process_folder(folder: Path) -> int:
    log_file = folder / "chat-log.md"
    if not log_file.exists():
        return 0

    lines = log_file.read_text(encoding="utf-8").splitlines(keepends=True)

    ids = []
    clean_lines = []
    for line in lines:
        m = COMMENT_RE.match(line.rstrip())
        if m:
            ids.append(m.group(1))
        else:
            clean_lines.append(line)

    if not ids:
        return 0

    if DRY_RUN:
        print(f"  [dry] {folder.name}: {len(ids)} comment lines to remove")
        return len(ids)

    # Append new IDs to seen_ids.txt
    ids_file = folder / "seen_ids.txt"
    existing_ids: set[str] = set()
    if ids_file.exists():
        existing_ids = set(ids_file.read_text(encoding="utf-8").splitlines())
    new_ids = [i for i in ids if i not in existing_ids]
    if new_ids:
        with ids_file.open("a", encoding="utf-8") as f:
            f.write("\n".join(new_ids) + "\n")

    # Write cleaned chat-log.md
    clean_text = "".join(clean_lines)
    # Collapse 3+ consecutive newlines to 2
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
    log_file.write_text(clean_text, encoding="utf-8")

    return len(ids)


def main():
    if not CRM_BASE.exists():
        print(f"❌ CRM path not found: {CRM_BASE}")
        sys.exit(1)

    if DRY_RUN:
        print("🔍 DRY RUN — no files will be changed\n")

    folders = [p for p in CRM_BASE.iterdir() if p.is_dir()]
    total_removed = 0
    affected = 0

    for folder in sorted(folders):
        count = process_folder(folder)
        if count:
            print(f"✅ {folder.name}: {count} comment lines removed")
            affected += 1
            total_removed += count

    print(f"\nDone. {affected} customer(s), {total_removed} comment lines removed.")


if __name__ == "__main__":
    main()
