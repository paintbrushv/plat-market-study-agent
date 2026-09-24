"""Parse OneSite (RealPage) Rent Roll Detail exports.

OneSite format characteristics:
- Multi-row header: rows 0-4 are report metadata, row 5 is the actual header
- Column: "Bldg/Unit" = unit_id, "Floorplan" = floorplan_code
- No explicit bed type column — derived from floorplan prefix (S=Studio, A=1BR, B=2BR, C=3BR)
- Contains "Former resident" and "Former applicant" rows that must be excluded
- Multiple rows per unit (current + historical) — deduplication keeps current row only

Usage:
    python etl/parse_onesite_rent_roll.py <input.xls> --output-dir <clean_dir> --summary
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# Optional pyarrow for parquet support
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_PARQUET = True
except ImportError:
    HAS_PARQUET = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# Floorplan prefix -> bed type mapping
FLOORPLAN_BED_PREFIX: dict[str, str] = {
    "S": "Studio",
    "A": "1BR",
    "B": "2BR",
    "C": "3BR",
    "D": "4BR",
    "E": "4BR",
    "PH": "2BR",  # Penthouse - may vary
}

# Active statuses to keep (everything else = skip)
ACTIVE_STATUSES = {
    "occupied",
    "occupied-ntv",
    "occupied-ntvl",
    "vacant",
    "vacant-leased",
    "admin/down",
    "model",
    "applicant",
    "pending renewal",
    "pending resident",
}

# Status normalization -> canonical
STATUS_MAP: dict[str, str] = {
    "occupied": "Occupied",
    "occupied-ntv": "Notice",
    "occupied-ntvl": "Notice",
    "pending renewal": "Occupied",
    "pending resident": "Vacant-Leased",
    "vacant": "Vacant",
    "vacant-leased": "Vacant-Leased",
    "admin/down": "Down",
    "model": "Model",
    "applicant": "Vacant-Leased",
}

# Canonical output columns
CANONICAL_COLUMNS = [
    "unit_id",
    "floorplan_code",
    "bed_type",
    "bath_count",
    "sqft",
    "market_rent",
    "lease_rent",
    "status",
    "resident_name",
    "move_in_date",
    "lease_start",
    "lease_end",
    "base_rent",
    "pet_rent",
    "parking_rent",
    "storage_rent",
    "utility_reimbursement",
    "other_income",
    "concessions",
    "total_rent",
]

FLOORPLAN_SUMMARY_COLUMNS = ["PlanCode", "BedType", "Units", "SqFt", "AvgMarketRent"]


def derive_bed_type(floorplan_code: str) -> str:
    """Derive bed type from OneSite floorplan code prefix."""
    if not floorplan_code:
        return ""
    # Extract letter prefix (strip trailing numbers, T, PH suffixes)
    prefix = re.match(r"^([A-Za-z]+)", floorplan_code)
    if not prefix:
        return ""
    letter = prefix.group(1)[0].upper()
    return FLOORPLAN_BED_PREFIX.get(letter, "")


def clean_currency(value: Any) -> float | None:
    """Extract numeric value from currency string or number."""
    if value is None or value == "" or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_date(value: Any) -> str:
    """Parse date to ISO format string."""
    if not value or (isinstance(value, float) and value != value):
        return ""
    value_str = str(value).strip()
    if not value_str:
        return ""
    for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"]:
        try:
            return datetime.strptime(value_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value_str


def read_onesite_xls(input_path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Read OneSite XLS file. Header is at row 5 (0-indexed)."""
    if not HAS_PANDAS:
        raise ImportError("pandas required: uv run pip install pandas xlrd")

    df = pd.read_excel(input_path, sheet_name=0, header=5, dtype=str)
    df = df.fillna("")

    # Strip newlines from column names
    df.columns = [str(c).replace("\n", " ").strip() for c in df.columns]
    headers = df.columns.tolist()
    rows = df.to_dict("records")
    return headers, rows


def transform_onesite_records(
    headers: list[str],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Transform raw OneSite rows to canonical format, deduplicating by unit_id."""
    # Build case-insensitive header map
    header_map = {h.lower().strip(): h for h in headers}

    def get_col(name: str) -> str | None:
        return header_map.get(name.lower().strip())

    # Column aliases
    UNIT_ALIASES = ["bldg/unit", "unit", "unit #", "apt"]
    FP_ALIASES = ["floorplan", "floor plan", "unit type"]
    SQFT_ALIASES = ["sqft", "sq. ft.", "sq ft", "square feet"]
    MKT_RENT_ALIASES = ["market rent", "market", "asking rent"]
    LEASE_RENT_ALIASES = ["lease rent", "rent", "actual rent"]
    STATUS_ALIASES = ["unit/lease status", "status", "unit status"]
    NAME_ALIASES = ["name", "resident name", "resident"]
    MOVEIN_ALIASES = ["move-in", "move in", "move in date"]
    LEASESTART_ALIASES = ["lease start", "start"]
    LEASEEND_ALIASES = ["lease end", "expiration"]

    # Income columns
    BASE_RENT_ALIASES = ["rent"]
    PET_RENT_ALIASES = ["petrent", "petfee"]
    PARKING_ALIASES = ["parking"]
    STORAGE_ALIASES = ["storage"]
    UTILITY_ALIASES = ["trashreimb", "trashadmin", "wdf"]
    OTHER_ALIASES = ["amenfee", "hometown", "mtom", "keys/locks"]
    CONCESSION_ALIASES = ["conc/specl", "concrenew", "emplcred", "ofcrcred", "conc/parking"]

    def find_col(aliases: list[str]) -> str | None:
        for alias in aliases:
            col = get_col(alias)
            if col:
                return col
        return None

    def get_val(row: dict, col: str | None) -> str:
        if col is None:
            return ""
        return str(row.get(col, "")).strip()

    def get_num(row: dict, col: str | None) -> float | None:
        return clean_currency(get_val(row, col))

    # Find column names once
    unit_col = find_col(UNIT_ALIASES)
    fp_col = find_col(FP_ALIASES)
    sqft_col = find_col(SQFT_ALIASES)
    mkt_col = find_col(MKT_RENT_ALIASES)
    lease_col = find_col(LEASE_RENT_ALIASES)
    status_col = find_col(STATUS_ALIASES)
    name_col = find_col(NAME_ALIASES)
    movein_col = find_col(MOVEIN_ALIASES)
    start_col = find_col(LEASESTART_ALIASES)
    end_col = find_col(LEASEEND_ALIASES)

    base_col = find_col(BASE_RENT_ALIASES)
    pet_col = find_col(PET_RENT_ALIASES)
    parking_col = find_col(PARKING_ALIASES)
    storage_col = find_col(STORAGE_ALIASES)
    utility_cols = [find_col([a]) for a in UTILITY_ALIASES if find_col([a])]
    other_cols = [find_col([a]) for a in OTHER_ALIASES if find_col([a])]
    concession_cols = [find_col([a]) for a in CONCESSION_ALIASES if find_col([a])]

    # Track best row per unit (prefer occupied > vacant > other)
    STATUS_PRIORITY = {
        "occupied": 10, "notice": 9, "vacant-leased": 8, "vacant": 7,
        "model": 6, "down": 5, "former resident": 0, "former applicant": 0
    }

    def status_priority(status_raw: str) -> int:
        s = status_raw.lower().strip()
        if s in ACTIVE_STATUSES:
            # Higher priority for occupied variants
            if "occupied" in s:
                return 10
            return STATUS_PRIORITY.get(s, 5)
        return 0

    unit_best: dict[str, dict] = {}

    for row in rows:
        unit_id_raw = get_val(row, unit_col)
        if not unit_id_raw or re.match(r"^(Totals|Total|Note|$)", unit_id_raw, re.IGNORECASE):
            continue

        # Clean unit_id: remove .0 suffix from Excel float formatting
        unit_id = re.sub(r"\.0$", "", unit_id_raw.strip())

        status_raw = get_val(row, status_col)
        status_lower = status_raw.lower().strip()

        # Skip former residents/applicants entirely
        if "former" in status_lower:
            continue

        # Skip if not active
        if status_lower not in ACTIVE_STATUSES:
            continue

        # Normalize status
        canonical_status = STATUS_MAP.get(status_lower, status_raw)

        # Floorplan
        fp_code = get_val(row, fp_col)
        bed_type = derive_bed_type(fp_code)

        # Sqft
        sqft_raw = get_val(row, sqft_col)
        sqft = int(float(sqft_raw)) if sqft_raw and sqft_raw != "" else None

        # Rents
        market_rent = get_num(row, mkt_col)
        lease_rent = get_num(row, lease_col)

        # Income breakdown
        base_rent = get_num(row, base_col)
        pet_rent = get_num(row, pet_col)
        parking_rent = get_num(row, parking_col)
        storage_rent = get_num(row, storage_col)
        utility_reimbursement = sum(get_num(row, c) or 0 for c in utility_cols) or None
        other_income = sum(get_num(row, c) or 0 for c in other_cols) or None
        concessions = sum(get_num(row, c) or 0 for c in concession_cols) or None

        # Total rent
        if base_rent is not None:
            income_sum = (
                (base_rent or 0)
                + (pet_rent or 0)
                + (parking_rent or 0)
                + (storage_rent or 0)
                + (utility_reimbursement or 0)
                + (other_income or 0)
            )
            concession_adj = -(concessions or 0) if (concessions or 0) > 0 else (concessions or 0)
            total_rent = income_sum + concession_adj
        else:
            total_rent = lease_rent

        record = {
            "unit_id": unit_id,
            "floorplan_code": fp_code,
            "bed_type": bed_type,
            "bath_count": None,
            "sqft": sqft,
            "market_rent": market_rent,
            "lease_rent": lease_rent,
            "status": canonical_status,
            "resident_name": get_val(row, name_col),
            "move_in_date": parse_date(get_val(row, movein_col)),
            "lease_start": parse_date(get_val(row, start_col)),
            "lease_end": parse_date(get_val(row, end_col)),
            "base_rent": base_rent,
            "pet_rent": pet_rent,
            "parking_rent": parking_rent,
            "storage_rent": storage_rent,
            "utility_reimbursement": utility_reimbursement,
            "other_income": other_income,
            "concessions": concessions,
            "total_rent": total_rent,
            "_status_priority": status_priority(status_raw),
        }

        # Keep best row per unit
        existing = unit_best.get(unit_id)
        if existing is None or record["_status_priority"] > existing["_status_priority"]:
            unit_best[unit_id] = record

    # Remove internal field and return sorted by unit_id
    records = []
    for rec in unit_best.values():
        rec.pop("_status_priority", None)
        records.append(rec)

    # Sort by unit_id (natural sort)
    def natural_sort_key(unit: str) -> tuple:
        parts = re.split(r"(\d+)", unit)
        return tuple(int(p) if p.isdigit() else p for p in parts)

    records.sort(key=lambda r: natural_sort_key(str(r.get("unit_id", ""))))
    return records


def generate_floorplan_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate to floorplan-level summary."""
    by_plan: dict[str, list[dict]] = {}
    for rec in records:
        plan = rec.get("floorplan_code") or "Unknown"
        by_plan.setdefault(plan, []).append(rec)

    summary = []
    for plan_code, units in sorted(by_plan.items()):
        bed_types = {u.get("bed_type") for u in units if u.get("bed_type")}
        bed_type = bed_types.pop() if bed_types else ""

        sqfts = [float(u["sqft"]) for u in units if u.get("sqft") is not None]
        avg_sqft = sum(sqfts) / len(sqfts) if sqfts else 0.0

        rents = [float(u["market_rent"]) for u in units if u.get("market_rent") is not None and u["market_rent"] > 0]
        avg_rent = sum(rents) / len(rents) if rents else 0.0

        summary.append({
            "PlanCode": plan_code,
            "BedType": bed_type,
            "Units": len(units),
            "SqFt": round(avg_sqft, 0),
            "AvgMarketRent": round(avg_rent, 2),
        })

    return summary


def write_csv(records: list[dict], path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def write_parquet(records: list[dict], path: Path, columns: list[str]) -> None:
    if not HAS_PARQUET:
        return
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows_clean = [{c: r.get(c) for c in columns} for r in records]
    table = pa.Table.from_pylist(rows_clean)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def extract_snapshot_date(input_path: Path) -> str:
    """Extract YYYY-MM snapshot date from filename."""
    name = input_path.stem
    # Pattern: M.DD.YY or MM.DD.YY (e.g., "4.27.26")
    m = re.search(r"(\d{1,2})\.(\d{2})\.(\d{2})$", name)
    if m:
        month = int(m.group(1))
        year = 2000 + int(m.group(3))
        return f"{year}-{month:02d}"
    # MM-DD-YYYY
    m = re.search(r"(\d{2})[-_](\d{2})[-_](\d{4})", name)
    if m:
        return f"{m.group(3)}-{m.group(1)}"
    return datetime.now().strftime("%Y-%m")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse OneSite Rent Roll Detail exports")
    parser.add_argument("input", type=Path, help="Input .xls/.xlsx file")
    parser.add_argument("--output-dir", type=Path, help="Output directory (clean/)")
    parser.add_argument("--output", "-o", type=Path, help="Output CSV path")
    parser.add_argument("--summary", "-s", action="store_true", help="Generate floorplan summary CSV")
    parser.add_argument("--summary-output", type=Path, help="Floorplan summary CSV path")
    parser.add_argument("--json", "-j", action="store_true", help="Also output JSON")
    parser.add_argument("--dry-run", action="store_true", help="Parse without writing")
    args = parser.parse_args()

    if not args.input.exists():
        raise FileNotFoundError(f"File not found: {args.input}")

    print(f"Parsing OneSite rent roll: {args.input}")
    headers, rows = read_onesite_xls(args.input)
    records = transform_onesite_records(headers, rows)
    print(f"Parsed {len(records)} active unit records (deduplicated)")

    if args.dry_run:
        print("\nSample records:")
        for rec in records[:5]:
            print(f"  {rec['unit_id']}: {rec['floorplan_code']} / {rec['bed_type']} / {rec['sqft']} sqft / ${rec['market_rent']} mkt / {rec['status']}")
        return

    # Determine output paths
    snapshot_date = extract_snapshot_date(args.input)
    print(f"Snapshot date: {snapshot_date}")

    if args.output_dir:
        clean_dir = args.output_dir
        out_csv = clean_dir / "rent_roll_standardized.csv"
        out_summary = clean_dir / "floorplan_summary.csv"
        snapshot_dir = clean_dir / "snapshots" / snapshot_date
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        history_dir = clean_dir / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_csv = args.output or args.input.parent / "rent_roll_standardized.csv"
        out_summary = args.summary_output or args.input.parent / "floorplan_summary.csv"
        snapshot_dir = None
        history_dir = None

    # Write main CSV
    write_csv(records, out_csv, CANONICAL_COLUMNS)
    print(f"Written: {out_csv}")

    # Snapshot copy
    if snapshot_dir:
        snap_csv = snapshot_dir / "rent_roll_standardized.csv"
        write_csv(records, snap_csv, CANONICAL_COLUMNS)
        print(f"Snapshot: {snap_csv}")

    # Write parquet history
    if history_dir and HAS_PARQUET:
        hist_path = history_dir / "rent_roll_history.parquet"
        write_parquet(records, hist_path, CANONICAL_COLUMNS)
        print(f"Parquet history: {hist_path}")

    # Floorplan summary
    if args.summary or args.output_dir:
        summary = generate_floorplan_summary(records)
        write_csv(summary, out_summary, FLOORPLAN_SUMMARY_COLUMNS)
        print(f"Floorplan summary: {out_summary}")

        if snapshot_dir:
            snap_sum = snapshot_dir / "floorplan_summary.csv"
            write_csv(summary, snap_sum, FLOORPLAN_SUMMARY_COLUMNS)

        if history_dir and HAS_PARQUET:
            fp_hist = history_dir / "floorplan_history.parquet"
            write_parquet(summary, fp_hist, FLOORPLAN_SUMMARY_COLUMNS)
            print(f"Floorplan parquet: {fp_hist}")

    if args.json:
        json_path = out_csv.with_suffix(".json")
        json_path.write_text(json.dumps(records, indent=2, default=str))
        print(f"JSON: {json_path}")

    # Summary stats
    occupied = [r for r in records if r["status"] in ("Occupied", "Notice")]
    vacant = [r for r in records if r["status"] in ("Vacant", "Vacant-Leased")]
    occ_pct = len(occupied) / len(records) * 100 if records else 0
    rents = [r["market_rent"] for r in records if r.get("market_rent") and r["market_rent"] > 0]
    avg_mkt = sum(rents) / len(rents) if rents else 0

    print(f"\n{'='*50}")
    print(f"  PARSE SUMMARY: {args.input.name}")
    print(f"{'='*50}")
    print(f"  Total Units:     {len(records)}")
    print(f"  Occupied:        {len(occupied)} ({occ_pct:.1f}%)")
    print(f"  Vacant:          {len(vacant)}")
    print(f"  Avg Market Rent: ${avg_mkt:,.0f}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
