#!/usr/bin/env python3
"""Customer tiering script — assigns S/A/B/C/D based on sales Excel data.

Reads a sales Excel file (销售明细表), calculates a weighted score per customer
based on total sales amount, order frequency, and average order value, then
updates HubSpot and optionally Feishu with the new tier.

Scoring formula:
  score = 销售额(50%) + 订单次数(20%) + 客单价(30%)

Tier thresholds:
  S ≥ 85 | A ≥ 65 | B ≥ 40 | C ≥ 20 | D < 20

Usage:
    cd whatsapp-crm-bridge
    python scripts/tier_customers.py path/to/sales.xlsx
    python scripts/tier_customers.py path/to/sales.xlsx --dry-run
    python scripts/tier_customers.py path/to/sales.xlsx --feishu
    python scripts/tier_customers.py path/to/sales.xlsx --months 12

Flags:
    --dry-run    Preview results without writing to any CRM
    --feishu     Also update Feishu 客户等级 field (requires field to exist in table)
    --months N   Only include sales from the last N months (default: 12)
    --output     Save tier report to a CSV file
"""

import argparse
import asyncio
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

# ── Bootstrap: add project root to path ──────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# CONFIGURABLE PARAMETERS — adjust here if tiers need re-calibration
# ══════════════════════════════════════════════════════════════════════

SCORE_WEIGHTS = {
    "sales_amount":    0.50,   # 50% — total sales in 万元
    "order_count":     0.20,   # 20% — number of distinct orders
    "avg_order_value": 0.30,   # 30% — average order value in 万元
}

# (threshold_万元_or_count, points_0_to_100)
SALES_BRACKETS = [
    (600, 100), (400, 85), (200, 70), (100, 50), (50, 35), (10, 20), (0, 5),
]
ORDER_COUNT_BRACKETS = [
    (30, 100), (15, 75), (5, 50), (2, 25), (1, 10), (0, 5),
]
AVG_ORDER_VALUE_BRACKETS = [  # in 万元
    (30, 100), (10, 70), (5, 45), (1, 20), (0, 5),
]

# (tier_name, minimum_score)  — highest score first
TIER_THRESHOLDS = [
    ("S", 85),
    ("A", 65),
    ("B", 40),
    ("C", 20),
    ("D", 0),
]

# Name match threshold for CRM lookup (0.0–1.0)
NAME_MATCH_THRESHOLD = 0.70

# Known Excel column aliases (auto-detected)
CUSTOMER_ALIASES   = ["客户", "客户名称", "客户名", "customer", "client", "name", "buyer"]
AMOUNT_ALIASES     = ["金额", "销售金额", "销售额", "合计", "总金额", "amount", "total", "price", "value"]
DATE_ALIASES       = ["日期", "销售日期", "date", "order_date", "时间", "created"]
ORDER_ID_ALIASES   = ["销售单号", "订单号", "单号", "order_id", "order_no", "编号", "id"]


# ══════════════════════════════════════════════════════════════════════
# SCORING LOGIC
# ══════════════════════════════════════════════════════════════════════

def _score_bracket(value: float, brackets: list[tuple]) -> float:
    """Return score for a value using the given bracket table."""
    for threshold, pts in brackets:
        if value >= threshold:
            return float(pts)
    return 0.0


def calculate_score(total_amount_wan: float, order_count: int, avg_value_wan: float) -> float:
    """Return weighted composite score (0–100)."""
    s = _score_bracket(total_amount_wan,  SALES_BRACKETS)
    o = _score_bracket(order_count,        ORDER_COUNT_BRACKETS)
    v = _score_bracket(avg_value_wan,      AVG_ORDER_VALUE_BRACKETS)
    return round(
        s * SCORE_WEIGHTS["sales_amount"] +
        o * SCORE_WEIGHTS["order_count"] +
        v * SCORE_WEIGHTS["avg_order_value"],
        1,
    )


def score_to_tier(score: float) -> str:
    """Convert composite score to tier letter."""
    for tier, threshold in TIER_THRESHOLDS:
        if score >= threshold:
            return tier
    return "D"


# ══════════════════════════════════════════════════════════════════════
# EXCEL READING
# ══════════════════════════════════════════════════════════════════════

def _find_column(df_cols: list[str], aliases: list[str]) -> str | None:
    """Return the first column name that matches any alias (case-insensitive)."""
    lower_cols = {c.lower(): c for c in df_cols}
    for alias in aliases:
        if alias.lower() in lower_cols:
            return lower_cols[alias.lower()]
    return None


def _detect_header_row(path: str) -> int:
    """Scan the first 15 rows to find the real column header row.

    Returns the 0-indexed row number to pass as `header=` to pd.read_excel.
    Falls back to 0 if no match found (standard Excel with no preamble).
    """
    all_aliases = (
        CUSTOMER_ALIASES + AMOUNT_ALIASES + DATE_ALIASES + ORDER_ID_ALIASES
    )
    aliases_lower = {a.lower() for a in all_aliases}

    # Read raw without header to inspect each row
    try:
        raw = pd.read_excel(path, header=None, nrows=15, dtype=str)
    except Exception:
        return 0

    for idx, row in raw.iterrows():
        row_vals = {str(v).strip().lower() for v in row if pd.notna(v) and str(v).strip()}
        matches = row_vals & aliases_lower
        if len(matches) >= 2:
            logger.info("Detected header row at index %d (matched: %s)", idx, matches)
            return int(idx)
    return 0


def load_sales_excel(path: str, months: int) -> pd.DataFrame:
    """Load and clean the sales Excel file.

    Handles Excel files with multi-row metadata preambles by auto-detecting
    the header row. Also forward-fills customer name and date, which are
    often only populated in the first line item of each order.

    Returns a DataFrame with columns: customer, amount, order_id, date.
    """
    logger.info("Reading Excel: %s", path)
    header_row = _detect_header_row(path)
    try:
        df = pd.read_excel(path, dtype=str, header=header_row)
    except Exception as e:
        logger.error("Failed to read Excel: %s", e)
        sys.exit(1)

    logger.info("Excel loaded: %d rows, columns: %s", len(df), list(df.columns))

    # Auto-detect columns
    cols = list(df.columns)
    col_customer  = _find_column(cols, CUSTOMER_ALIASES)
    col_amount    = _find_column(cols, AMOUNT_ALIASES)
    col_date      = _find_column(cols, DATE_ALIASES)
    col_order_id  = _find_column(cols, ORDER_ID_ALIASES)

    if not col_customer:
        logger.error("Cannot find customer column. Available: %s", cols)
        logger.error("Expected one of: %s", CUSTOMER_ALIASES)
        sys.exit(1)
    if not col_amount:
        logger.error("Cannot find amount column. Available: %s", cols)
        logger.error("Expected one of: %s", AMOUNT_ALIASES)
        sys.exit(1)

    logger.info(
        "Column mapping → customer='%s', amount='%s', date='%s', order_id='%s'",
        col_customer, col_amount, col_date, col_order_id,
    )

    # Forward-fill customer name, date, order_id — same order's line items
    # often only have these values in the first row.
    for col in [col_customer, col_date, col_order_id]:
        if col and col in df.columns:
            # Replace empty strings with NaN so ffill works properly
            df[col] = df[col].replace({"": pd.NA, "nan": pd.NA, "NaN": pd.NA})
            df[col] = df[col].ffill()

    # Build clean DataFrame
    result = pd.DataFrame()
    result["customer"] = df[col_customer].astype(str).str.strip()
    result["amount"]   = pd.to_numeric(df[col_amount], errors="coerce").fillna(0)

    if col_date:
        result["date"] = pd.to_datetime(df[col_date], errors="coerce")
    else:
        result["date"] = pd.NaT
        logger.warning("No date column found — cannot filter by months")

    if col_order_id:
        result["order_id"] = df[col_order_id].astype(str).str.strip()
    else:
        result["order_id"] = ""

    # Drop rows with empty/invalid customer or zero amount
    result = result[
        result["customer"].notna()
        & (~result["customer"].isin(["", "nan", "None", "NaN"]))
        & (result["amount"] > 0)
    ]

    # Filter by date range
    if months > 0 and not result["date"].isna().all():
        cutoff = datetime.now() - timedelta(days=months * 30)
        before = len(result)
        result = result[result["date"].isna() | (result["date"] >= cutoff)]
        logger.info("Date filter (last %d months): %d → %d rows", months, before, len(result))

    logger.info("Clean data: %d rows, %d unique customers",
                len(result), result["customer"].nunique())
    return result


# ══════════════════════════════════════════════════════════════════════
# AGGREGATION
# ══════════════════════════════════════════════════════════════════════

def aggregate_customers(df: pd.DataFrame) -> list[dict]:
    """Group by customer and compute tier metrics.

    Returns list of dicts sorted by score descending.
    """
    rows = []
    for customer, group in df.groupby("customer"):
        if not customer or customer.lower() in ("nan", "none", ""):
            continue

        total_amount = group["amount"].sum()

        # Count distinct orders (by order_id if available, otherwise by row)
        if group["order_id"].str.strip().ne("").any():
            order_count = group["order_id"].nunique()
        else:
            order_count = len(group)

        avg_order_value = total_amount / max(order_count, 1)

        # Convert to 万元 for scoring
        total_wan  = total_amount / 10000
        avg_wan    = avg_order_value / 10000

        score = calculate_score(total_wan, order_count, avg_wan)
        tier  = score_to_tier(score)

        rows.append({
            "customer":          customer,
            "total_amount":      round(total_amount, 2),
            "total_amount_wan":  round(total_wan, 2),
            "order_count":       order_count,
            "avg_order_value":   round(avg_order_value, 2),
            "avg_order_wan":     round(avg_wan, 2),
            "score":             score,
            "tier":              tier,
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


# ══════════════════════════════════════════════════════════════════════
# CRM MATCHING
# ══════════════════════════════════════════════════════════════════════

def _name_score(a: str, b: str) -> float:
    """Fuzzy name similarity (0.0–1.0)."""
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def build_hubspot_name_index(contacts: list[dict]) -> dict[str, dict]:
    """Index HubSpot contacts by full name (lower) for fast lookup."""
    index: dict[str, dict] = {}
    for c in contacts:
        first = (c.get("firstname") or "").strip()
        last  = (c.get("lastname") or "").strip()
        full  = f"{first} {last}".strip() if (first or last) else ""
        if full:
            index[full.lower()] = c
        # Also index by firstname alone if no lastname
        if first and not last:
            index[first.lower()] = c
    return index


def match_hubspot(customer_name: str, index: dict[str, dict], contacts: list[dict]) -> dict | None:
    """Find the best-matching HubSpot contact for a customer name."""
    # Exact match first
    key = customer_name.lower().strip()
    if key in index:
        return index[key]

    # Fuzzy match
    best_score = 0.0
    best_contact = None
    for contact in contacts:
        first = (contact.get("firstname") or "").strip()
        last  = (contact.get("lastname") or "").strip()
        full  = f"{first} {last}".strip() if (first or last) else first
        if not full:
            continue
        s = _name_score(customer_name, full)
        if s > best_score:
            best_score = s
            best_contact = contact

    if best_score >= NAME_MATCH_THRESHOLD and best_contact:
        return best_contact
    return None


# ══════════════════════════════════════════════════════════════════════
# HUBSPOT UPDATE
# ══════════════════════════════════════════════════════════════════════

async def update_hubspot_tiers(
    tier_rows: list[dict],
    dry_run: bool,
) -> dict:
    """Fetch all HubSpot contacts, match by name, update customer_tier."""
    from app.writers.hubspot_writer import list_all_contacts, update_contact

    logger.info("Fetching all HubSpot contacts...")
    contacts = await list_all_contacts()
    logger.info("HubSpot: %d contacts loaded", len(contacts))

    name_index = build_hubspot_name_index(contacts)

    stats = {"updated": 0, "unchanged": 0, "not_found": 0, "errors": 0}
    changes = []

    for row in tier_rows:
        cname = row["customer"]
        new_tier = row["tier"]

        contact = match_hubspot(cname, name_index, contacts)
        if not contact:
            logger.debug("HubSpot: no match for '%s'", cname)
            stats["not_found"] += 1
            continue

        contact_id  = contact["id"]
        old_tier    = contact.get("customer_tier") or ""
        first = (contact.get("firstname") or "").strip()
        last  = (contact.get("lastname") or "").strip()
        hs_name = f"{first} {last}".strip() or first

        if old_tier == new_tier:
            stats["unchanged"] += 1
            continue

        change = {
            "customer":   cname,
            "hs_name":    hs_name,
            "contact_id": contact_id,
            "old_tier":   old_tier or "—",
            "new_tier":   new_tier,
            "score":      row["score"],
            "amount_wan": row["total_amount_wan"],
        }
        changes.append(change)

        if not dry_run:
            try:
                await update_contact(contact_id, extra={"customer_tier": new_tier})
                stats["updated"] += 1
                logger.info("HubSpot updated: '%s' → %s (was %s)", hs_name, new_tier, old_tier or "—")
            except Exception as e:
                logger.error("HubSpot update failed for '%s': %s", cname, e)
                stats["errors"] += 1
        else:
            stats["updated"] += 1

    stats["changes"] = changes
    return stats


# ══════════════════════════════════════════════════════════════════════
# FEISHU UPDATE
# ══════════════════════════════════════════════════════════════════════

async def update_feishu_tiers(
    tier_rows: list[dict],
    dry_run: bool,
) -> dict:
    """Match customers in Feishu and update 客户等级 field.

    Requires a text field named '客户等级' to exist in the Feishu customer table.
    """
    from app.writers.feishu_writer import _search_records, _update_record
    from app.config import settings

    stats = {"updated": 0, "unchanged": 0, "not_found": 0, "errors": 0}

    for row in tier_rows:
        cname    = row["customer"]
        new_tier = row["tier"]

        try:
            # Search Feishu by customer name
            items = await _search_records(
                settings.feishu_table_customers,
                field_name="客户",
                value=cname,
            )
            if not items:
                stats["not_found"] += 1
                logger.debug("Feishu: no match for '%s'", cname)
                continue

            record = items[0]
            record_id = record.get("record_id", "")
            old_tier  = record.get("fields", {}).get("客户等级", "") or ""

            if old_tier == new_tier:
                stats["unchanged"] += 1
                continue

            if not dry_run:
                await _update_record(
                    settings.feishu_table_customers,
                    record_id,
                    {"客户等级": new_tier},
                )
                stats["updated"] += 1
                logger.info("Feishu updated: '%s' → %s (was %s)", cname, new_tier, old_tier or "—")
            else:
                stats["updated"] += 1

        except Exception as e:
            logger.error("Feishu update failed for '%s': %s", cname, e)
            stats["errors"] += 1

    return stats


# ══════════════════════════════════════════════════════════════════════
# REPORT PRINTING
# ══════════════════════════════════════════════════════════════════════

def print_tier_summary(tier_rows: list[dict]) -> None:
    """Print a summary table of all customer tiers."""
    tier_groups: dict[str, list] = defaultdict(list)
    for row in tier_rows:
        tier_groups[row["tier"]].append(row)

    print("\n" + "="*70)
    print("  CUSTOMER TIER REPORT")
    print("="*70)

    for tier, _ in TIER_THRESHOLDS:
        group = tier_groups.get(tier, [])
        stars = {"S": "⭐⭐⭐⭐⭐", "A": "⭐⭐⭐⭐", "B": "⭐⭐⭐", "C": "⭐⭐", "D": "⭐"}.get(tier, "")
        print(f"\n  {tier} {stars}  ({len(group)} customers)")
        print(f"  {'Customer':<30} {'Sales(万)':<12} {'Orders':<8} {'Avg(万)':<10} {'Score'}")
        print(f"  {'-'*65}")
        for row in group[:20]:  # top 20 per tier
            print(
                f"  {row['customer']:<30} "
                f"{row['total_amount_wan']:<12.1f} "
                f"{row['order_count']:<8} "
                f"{row['avg_order_wan']:<10.1f} "
                f"{row['score']}"
            )
        if len(group) > 20:
            print(f"  ... and {len(group)-20} more")

    print("\n" + "="*70)
    tier_dist = {t: len(tier_groups.get(t, [])) for t, _ in TIER_THRESHOLDS}
    total = sum(tier_dist.values())
    print(f"  Total: {total} customers | " + " | ".join(f"{t}:{n}" for t, n in tier_dist.items()))
    print("="*70 + "\n")


def print_changes(changes: list[dict]) -> None:
    """Print the list of tier changes."""
    if not changes:
        print("\n  No tier changes detected.\n")
        return

    print(f"\n  TIER CHANGES ({len(changes)} contacts):")
    print(f"  {'Customer':<30} {'Old':<6} {'New':<6} {'Score':<8} {'Sales(万)'}")
    print(f"  {'-'*60}")
    for c in sorted(changes, key=lambda x: x["new_tier"]):
        arrow = "↑" if c["old_tier"] < c["new_tier"] else "↓"
        print(
            f"  {c['customer']:<30} "
            f"{c['old_tier']:<6} "
            f"{arrow} {c['new_tier']:<4} "
            f"{c['score']:<8} "
            f"{c['amount_wan']:.1f}"
        )
    print()


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

async def main(args: argparse.Namespace) -> None:
    # 1. Load and aggregate sales data
    df = load_sales_excel(args.excel, args.months)
    tier_rows = aggregate_customers(df)

    # 2. Print tier summary
    print_tier_summary(tier_rows)

    if args.dry_run:
        print("  [DRY RUN MODE — no CRM changes will be made]\n")

    # 3. Update HubSpot
    logger.info("Updating HubSpot customer_tier...")
    hs_stats = await update_hubspot_tiers(tier_rows, dry_run=args.dry_run)

    print_changes(hs_stats.get("changes", []))
    print(
        f"  HubSpot: {hs_stats['updated']} updated, "
        f"{hs_stats['unchanged']} unchanged, "
        f"{hs_stats['not_found']} not found, "
        f"{hs_stats['errors']} errors"
    )

    # 4. Optionally update Feishu
    if args.feishu:
        logger.info("Updating Feishu 客户等级...")
        fs_stats = await update_feishu_tiers(tier_rows, dry_run=args.dry_run)
        print(
            f"  Feishu:  {fs_stats['updated']} updated, "
            f"{fs_stats['unchanged']} unchanged, "
            f"{fs_stats['not_found']} not found, "
            f"{fs_stats['errors']} errors"
        )
    else:
        print("  Feishu:  skipped (add --feishu to also update Feishu)")

    # 5. Optional CSV output
    if args.output:
        import csv
        out_path = Path(args.output)
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "customer", "tier", "score",
                "total_amount_wan", "order_count", "avg_order_wan",
            ])
            writer.writeheader()
            for row in tier_rows:
                writer.writerow({
                    "customer":         row["customer"],
                    "tier":             row["tier"],
                    "score":            row["score"],
                    "total_amount_wan": row["total_amount_wan"],
                    "order_count":      row["order_count"],
                    "avg_order_wan":    row["avg_order_wan"],
                })
        logger.info("Tier report saved to %s", out_path)

    if args.dry_run:
        print("\n  [DRY RUN — re-run without --dry-run to apply changes]\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Calculate and update customer tiers from sales Excel data."
    )
    parser.add_argument("excel", help="Path to sales Excel file (销售明细表)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only — no CRM updates",
    )
    parser.add_argument(
        "--feishu", action="store_true",
        help="Also update Feishu 客户等级 field (requires field to exist in table)",
    )
    parser.add_argument(
        "--months", type=int, default=12,
        help="Only include sales from the last N months (default: 12, 0=all)",
    )
    parser.add_argument(
        "--output", type=str, default="",
        help="Save tier report to a CSV file",
    )
    args = parser.parse_args()

    if not Path(args.excel).exists():
        print(f"Error: Excel file not found: {args.excel}")
        sys.exit(1)

    asyncio.run(main(args))
