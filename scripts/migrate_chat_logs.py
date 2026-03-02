#!/usr/bin/env python3
"""Migrate per-day chat-log-YYYY-MM-DD.md files into a single chat-log.md per customer.

Run from project root:
    python scripts/migrate_chat_logs.py [--dry-run]
"""

import re
import sys
from pathlib import Path

CRM_BASE = (
    Path.home()
    / "Nutstore Files/我的坚果云/LuckyOS/LOCA-Factory-Brain/05-Sales Library/CRM"
)
DRY_RUN = "--dry-run" in sys.argv

# Matches:  [HH:MM:SS] <<< text   or   [HH:MM:SS] >>> text
MSG_RE = re.compile(r"^(\[\d{2}:\d{2}:\d{2}\]) (<<<|>>>) (.*)$")


def migrate_folder(folder: Path) -> int:
    """Merge all chat-log-YYYY-MM-DD.md files into chat-log.md. Returns messages merged."""
    daily_files = sorted(folder.glob("chat-log-????-??-??.md"))
    if not daily_files:
        return 0

    # Get customer name from any existing frontmatter
    customer_label = folder.name
    for f in daily_files:
        m = re.search(r"^customer:\s*(.+)$", f.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            customer_label = m.group(1).strip()
            break

    # Build merged content
    merged_lines: list[str] = []
    merged_lines.append(f"---\ntype: chat-log\ncustomer: {customer_label}\n---\n\n")
    merged_lines.append(f"# Chat Log - {customer_label}\n")

    total = 0
    for daily_file in daily_files:
        date_str = daily_file.stem.removeprefix("chat-log-")
        text = daily_file.read_text(encoding="utf-8")

        # Collect message lines + their id comments in order
        lines = text.splitlines()
        block: list[str] = []
        date_written = False

        for i, raw in enumerate(lines):
            m = MSG_RE.match(raw)
            if m:
                time_part, arrow, content = m.group(1), m.group(2), m.group(3)
                sender = "Lucky" if arrow == ">>>" else customer_label
                # Peek at next line for the comment id
                comment = ""
                if i + 1 < len(lines) and lines[i + 1].startswith("<!-- "):
                    comment = lines[i + 1]

                if not date_written:
                    block.append(f"\n## {date_str}\n")
                    date_written = True

                block.append(f"{time_part} {sender}: {content}")
                if comment:
                    block.append(comment)
                total += 1
            # skip frontmatter, headers, blank lines, raw comment lines handled above

        merged_lines.extend(block)

    merged_content = "\n".join(merged_lines) + "\n"
    dest = folder / "chat-log.md"

    if DRY_RUN:
        print(f"  [dry] would write {total} messages → {dest.name}")
        return total

    dest.write_text(merged_content, encoding="utf-8")

    # Remove old daily files
    for f in daily_files:
        f.unlink()

    return total


def main():
    if not CRM_BASE.exists():
        print(f"❌ CRM path not found: {CRM_BASE}")
        sys.exit(1)

    if DRY_RUN:
        print("🔍 DRY RUN — no files will be changed\n")

    folders = [p for p in CRM_BASE.iterdir() if p.is_dir()]
    total_msgs = 0
    affected = 0

    for folder in sorted(folders):
        count = migrate_folder(folder)
        if count:
            print(f"✅ {folder.name}: {count} messages merged")
            affected += 1
            total_msgs += count

    print(f"\nDone. {affected} customer(s), {total_msgs} message(s) merged into single files.")


if __name__ == "__main__":
    main()
