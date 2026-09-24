"""Parse rent roll with vertical charge layout (one charge per row).

This parser handles rent roll formats where:
- Unit info is on a main row (unit ID, type, sqft, resident, status, market rent)
- Charges are listed vertically on subsequent rows (Description, Amount columns)
- "Total" row marks the end of charges for that unit

This format is common in some property management systems where other income
and lease charges are displayed as a vertical list rather than horizontal columns.

Usage:
    python etl/parse_vertical_charges.py input.xls --config tower_village --summary
    python etl/parse_vertical_charges.py input.xls --auto-detect --summary
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

CONFIGS_DIR = Path("configs/vertical_charges")

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

FLOORPLAN_SUMMARY_COLUMNS = [
    "PlanCode",
    "BedType",
    "Units",
    "SqFt",
    "AvgMarketRent",
]

# Default charge classification rules (case-insensitive substring matching)
DEFAULT_CHARGE_RULES = {
    "rent": ["rent"],
    "pet": ["pet rent", "pet fee", "petrent"],
    "parking": ["parking", "reserved parking", "carport", "garage"],
    "storage": ["storage"],
    "utility": [
        "trash", "water", "sewer", "pest control", "gas",
        "electric", "utility", "rubs", "cable", "internet",
        "facility fee"
    ],
    "concession": [
        "concession", "special", "discount", "credit",
        "off monthly", "move in special", "off every", "price of"
    ],
    "other": [
        "renter", "damage waiver", "popic", "amenity",
        "month to month", "mtm", "admin"
    ],
}


def load_config(config_name: str) -> dict[str, Any]:
    """Load a property configuration by name."""
    config_path = CONFIGS_DIR / f"{config_name}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with config_path.open(encoding="utf-8") as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def list_available_configs() -> list[str]:
    """List all available property configurations."""
    if not CONFIGS_DIR.exists():
        return []
    configs = []
    for path in CONFIGS_DIR.glob("*.yaml"):
        if not path.name.startswith("_"):
            configs.append(path.stem)
    return sorted(configs)


def detect_property_from_file(df: pd.DataFrame) -> str | None:
    """Try to detect which property config to use from file content."""
    for i in range(min(10, len(df))):
        for j in range(min(15, len(df.columns))):
            cell = df.iloc[i, j]
            if pd.isna(cell):
                continue
            cell_str = str(cell).lower()

            for config_name in list_available_configs():
                try:
                    config = load_config(config_name)
                    prop_id = config.get("property_id", "").lower()
                    prop_name = config.get("property_name", "").lower()
                    # Also check aliases
                    aliases = config.get("property_aliases", [])

                    if prop_id and prop_id in cell_str:
                        return config_name
                    if prop_name and prop_name in cell_str:
                        return config_name
                    for alias in aliases:
                        if alias.lower() in cell_str:
                            return config_name
                except Exception:
                    continue
    return None


def classify_charge(description: str, rules: dict[str, list[str]]) -> tuple[str, bool]:
    """
    Classify a charge description into a category.

    Returns:
        tuple: (category, is_concession)
        category: 'rent', 'pet', 'parking', 'storage', 'utility', 'concession', 'other'
        is_concession: True if this is a discount/concession (typically negative)
    """
    desc_lower = description.lower()

    # Check concessions first (they may contain "rent" but are discounts)
    for keyword in rules.get("concession", DEFAULT_CHARGE_RULES["concession"]):
        if keyword.lower() in desc_lower:
            return "concession", True

    # Check each category
    for category in ["rent", "pet", "parking", "storage", "utility", "other"]:
        keywords = rules.get(category, DEFAULT_CHARGE_RULES.get(category, []))
        for keyword in keywords:
            if keyword.lower() in desc_lower:
                return category, False

    # Default to other
    return "other", False


def apply_bed_type_rules(floorplan_code: str, rules: list[dict[str, str]]) -> str:
    """Apply bed type rules from config to determine bed type."""
    if not floorplan_code:
        return ""

    code_upper = floorplan_code.upper()
    # Strip common suffixes for matching
    code_clean = re.sub(r"[-_]\d+$", "", code_upper)  # Remove trailing -1, -2, etc.

    for rule in rules:
        match_type = rule.get("match_type", "prefix")
        pattern = rule.get("pattern", "")
        bed_type = rule.get("bed_type", "")

        if match_type == "prefix":
            if code_clean.startswith(pattern.upper()):
                return bed_type
        elif match_type == "suffix":
            if code_clean.endswith(pattern.upper()):
                return bed_type
        elif match_type == "contains":
            if pattern.upper() in code_clean:
                return bed_type
        elif match_type == "regex":
            match = re.search(pattern, code_clean, re.IGNORECASE)
            if match:
                result = bed_type
                for i, group in enumerate(match.groups(), 1):
                    if group:
                        result = result.replace(f"{{{i}}}", group)
                return result

    return ""


def format_date(d: datetime | str | float | None) -> str:
    """Format date value to ISO format."""
    if pd.isna(d):
        return ""
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    return str(d)


def parse_rent_roll(
    input_path: Path,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Parse rent roll with vertical charge layout.

    The algorithm:
    1. Scan file for unit rows (identified by unit ID in col 0)
    2. For each unit row, collect all charge rows until hitting next unit or Total
    3. Classify each charge and sum into income categories
    """
    df = pd.read_excel(input_path, header=None)

    # Get column indices from config or use defaults
    cols = config.get("columns", {}) if config else {}
    col_unit = cols.get("unit_id", 0)
    col_type = cols.get("floorplan", 2)
    col_sqft = cols.get("sqft", 4)
    col_resident = cols.get("resident", 5)
    col_status = cols.get("status", 11)
    col_market = cols.get("market_rent", 13)
    col_desc = cols.get("charge_description", 20)
    col_amount = cols.get("charge_amount", 23)
    # charge_amount may be an int or a list of ints (summed across columns)
    amount_cols = col_amount if isinstance(col_amount, list) else [col_amount]
    skip_charge_descs = [
        s.lower() for s in (config.get("skip_charge_descriptions", []) if config else [])
    ]
    col_move_in = cols.get("move_in", 27)
    col_lease_start = cols.get("lease_start", 29)
    col_lease_end = cols.get("lease_end", 30)

    # Get charge classification rules
    charge_rules = config.get("charge_rules", DEFAULT_CHARGE_RULES) if config else DEFAULT_CHARGE_RULES

    # Get bed type rules
    bed_type_rules = config.get("bed_type_rules", []) if config else []

    # Get status mappings
    status_map = config.get("status_mappings", {
        "C": "Occupied",
        "NTV": "Notice",
        "UE": "Occupied",  # Employee unit
    }) if config else {"C": "Occupied", "NTV": "Occupied", "UE": "Occupied"}

    # Skip patterns for unit ID detection
    skip_patterns = config.get("skip_patterns", [
        "unit", "rent roll", "as of", "total", "current", "printed"
    ]) if config else ["unit", "rent roll", "as of", "total", "current", "printed"]

    # Optional: stop collecting unit rows once a section header matches (e.g., "Future
    # Residents/Applicants"). Scanned in col_unit as a case-insensitive substring.
    stop_section_patterns = config.get("stop_section_patterns", []) if config else []

    # Optional: detect floorplan from section headers (e.g. "Unit Type: Beal").
    # When set, the parser tracks the most-recent section header and uses it as
    # the floorplan_code for subsequent unit rows. Used by section-header-style
    # rent rolls where the floorplan is NOT a column on the unit row.
    section_floorplan_pattern = (
        config.get("section_floorplan_pattern") if config else None
    )

    # Optional: regex pattern for valid unit IDs. Defaults to the historical
    # legacy no-dash unit-ID pattern (no dashes). Some properties
    # use Bldg-Unit IDs (e.g. "4A-402") and need a different pattern.
    unit_id_pattern = (
        config.get("unit_id_pattern") if config else None
    ) or r"^[A-Z]?\d+[A-Z]?$"
    allow_dash_in_unit_id = bool(config.get("allow_dash_in_unit_id", False)) if config else False

    units: list[dict[str, Any]] = []

    # First pass: build a row -> floorplan map from section headers, if configured.
    row_floorplan: dict[int, str] = {}
    if section_floorplan_pattern:
        current_fp = ""
        section_re = re.compile(section_floorplan_pattern)
        for i, row in df.iterrows():
            cell = row.iloc[col_unit]
            if pd.notna(cell):
                m = section_re.match(str(cell).strip())
                if m:
                    current_fp = m.group(1).strip() if m.groups() else str(cell).strip()
            row_floorplan[i] = current_fp

    # Find all unit rows first
    unit_rows: list[int] = []
    for i, row in df.iterrows():
        unit_id = row.iloc[col_unit]
        if pd.isna(unit_id):
            continue
        unit_str = str(unit_id).strip()

        # Stop at section headers that mark non-current sections (applicants, summaries)
        if stop_section_patterns:
            stop = False
            for pattern in stop_section_patterns:
                if pattern.lower() in unit_str.lower():
                    stop = True
                    break
            if stop:
                break

        # Skip if matches skip pattern
        skip = False
        for pattern in skip_patterns:
            if pattern.lower() in unit_str.lower():
                skip = True
                break
        if skip:
            continue

        # Check if it looks like a unit ID
        if re.match(unit_id_pattern, unit_str, re.IGNORECASE):
            # legacy convention: dashes mean floorplan-code-misread.
            # Newer configs that use Bldg-Unit IDs override this with allow_dash_in_unit_id.
            if allow_dash_in_unit_id or '-' not in unit_str:
                unit_rows.append(i)

    # Process each unit
    for idx, row_num in enumerate(unit_rows):
        row = df.iloc[row_num]

        # Determine end of this unit's charge rows
        if idx + 1 < len(unit_rows):
            end_row = unit_rows[idx + 1]
        else:
            end_row = len(df)

        # Extract unit info
        unit_id = str(row.iloc[col_unit]).strip()
        # Prefer section-header-derived floorplan (section-header-style) when configured;
        # otherwise fall back to the per-row floorplan column.
        floorplan = ""
        if section_floorplan_pattern:
            floorplan = row_floorplan.get(row_num, "")
        if not floorplan and col_type < len(row) and pd.notna(row.iloc[col_type]):
            floorplan = str(row.iloc[col_type]).strip()
        sqft = int(row.iloc[col_sqft]) if pd.notna(row.iloc[col_sqft]) else None
        resident = str(row.iloc[col_resident]).strip() if pd.notna(row.iloc[col_resident]) else ""
        if col_status < len(row) and pd.notna(row.iloc[col_status]):
            status_raw = str(row.iloc[col_status]).strip()
        else:
            status_raw = ""
        market_rent = float(row.iloc[col_market]) if pd.notna(row.iloc[col_market]) else None

        # Parse dates
        move_in = format_date(row.iloc[col_move_in])
        lease_start = format_date(row.iloc[col_lease_start])
        lease_end = format_date(row.iloc[col_lease_end])

        # Determine status
        if "vacant" in resident.lower():
            status = "Vacant"
            resident = ""
        elif status_raw in status_map:
            status = status_map[status_raw]
        elif status_raw:
            status = status_raw
        else:
            status = "Occupied" if resident else "Vacant"

        # Determine bed type
        bed_type = apply_bed_type_rules(floorplan, bed_type_rules)
        floorplan_mappings = config.get("floorplan_mappings", {}) if config else {}
        mapped_floorplan = floorplan_mappings.get(floorplan, {}) if floorplan else {}
        if mapped_floorplan:
            bed_type = mapped_floorplan.get("bed_type", bed_type)
            mapped_sqft = mapped_floorplan.get("sqft", mapped_floorplan.get("expected_sqft"))
            if mapped_sqft is not None:
                sqft = int(mapped_sqft)
            mapped_bath = mapped_floorplan.get("bath_count")
            bath_count = float(mapped_bath) if mapped_bath is not None else ""
        else:
            bath_count = ""


        # Initialize income buckets
        income = {
            "base_rent": 0.0,
            "pet_rent": 0.0,
            "parking_rent": 0.0,
            "storage_rent": 0.0,
            "utility_reimbursement": 0.0,
            "other_income": 0.0,
            "concessions": 0.0,
        }

        # Process charges - start with the unit row itself (it may have first charge)
        for charge_row_num in range(row_num, end_row):
            charge_row = df.iloc[charge_row_num]
            desc = charge_row.iloc[col_desc]

            if pd.isna(desc) or not str(desc).strip():
                continue

            desc_str = str(desc).strip()
            desc_lower = desc_str.lower()

            # Skip Total row
            if desc_lower == "total":
                continue

            # Skip configured noise descriptions (header repeats, etc.)
            if any(skip in desc_lower for skip in skip_charge_descs):
                continue

            # Sum amount across one or more columns
            amt = 0.0
            valid = False
            for c in amount_cols:
                if c >= len(charge_row):
                    continue
                v = charge_row.iloc[c]
                if pd.isna(v):
                    continue
                try:
                    amt += float(v)
                    valid = True
                except (ValueError, TypeError):
                    continue
            if not valid:
                continue

            # Classify and add to appropriate bucket
            category, is_concession = classify_charge(desc_str, charge_rules)

            if category == "rent":
                income["base_rent"] += amt
            elif category == "pet":
                income["pet_rent"] += amt
            elif category == "parking":
                income["parking_rent"] += amt
            elif category == "storage":
                income["storage_rent"] += amt
            elif category == "utility":
                income["utility_reimbursement"] += amt
            elif category == "concession" or is_concession:
                # Concessions are typically negative, but store as positive
                income["concessions"] += abs(amt)
            else:  # other
                income["other_income"] += amt

        # Calculate total rent
        total_rent = (
            income["base_rent"]
            + income["pet_rent"]
            + income["parking_rent"]
            + income["storage_rent"]
            + income["utility_reimbursement"]
            + income["other_income"]
            - income["concessions"]
        )

        units.append({
            "unit_id": unit_id,
            "floorplan_code": floorplan,
            "bed_type": bed_type,
            "bath_count": bath_count,
            "sqft": sqft,
            "market_rent": market_rent,
            "lease_rent": income["base_rent"] if income["base_rent"] > 0 else "",
            "status": status,
            "resident_name": resident if status != "Vacant" else "",
            "move_in_date": move_in,
            "lease_start": lease_start,
            "lease_end": lease_end,
            "base_rent": round(income["base_rent"], 2),
            "pet_rent": round(income["pet_rent"], 2),
            "parking_rent": round(income["parking_rent"], 2),
            "storage_rent": round(income["storage_rent"], 2),
            "utility_reimbursement": round(income["utility_reimbursement"], 2),
            "other_income": round(income["other_income"], 2),
            "concessions": round(income["concessions"], 2),
            "total_rent": round(total_rent, 2),
        })

    return units


def generate_floorplan_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate records into floorplan summary format."""
    by_plan: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        plan = rec.get("floorplan_code") or "Unknown"
        by_plan[plan].append(rec)

    summary = []
    for plan_code, plan_units in sorted(by_plan.items()):
        beds = {u.get("bed_type") for u in plan_units if u.get("bed_type")}
        bed_type = list(beds)[0] if beds else ""

        sqfts = [u["sqft"] for u in plan_units if u.get("sqft")]
        avg_sqft = sum(sqfts) / len(sqfts) if sqfts else 0

        rents = [u["market_rent"] for u in plan_units if u.get("market_rent")]
        avg_rent = sum(rents) / len(rents) if rents else 0

        summary.append({
            "PlanCode": plan_code,
            "BedType": bed_type,
            "Units": len(plan_units),
            "SqFt": round(avg_sqft),
            "AvgMarketRent": round(avg_rent, 2),
        })

    return summary


def write_csv(records: list[dict[str, Any]], output_path: Path, columns: list[str]) -> None:
    """Write records to CSV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def extract_snapshot_date(input_path: Path, df: pd.DataFrame | None = None) -> str:
    """Extract snapshot date from filename or file content."""
    filename = input_path.stem

    # Pattern: YYYY.MM.DD or YYYY-MM-DD or YYYY_MM_DD (check first — unambiguous)
    match = re.search(r"(\d{4})[.\-_](\d{2})[.\-_](\d{2})", filename)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    # Pattern: M.DD.YY or MM.DD.YY (e.g., "Tower Village RR 1.13.26")
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", filename)
    if match:
        month = match.group(1).zfill(2)
        year = match.group(3)
        if len(year) == 2:
            year = "20" + year
        return f"{year}-{month}"

    # Pattern: MM-DD-YYYY or MM_DD_YYYY
    match = re.search(r"(\d{2})[-_](\d{2})[-_](\d{4})", filename)
    if match:
        return f"{match.group(3)}-{match.group(1)}"

    # Try file content
    if df is None:
        df = pd.read_excel(input_path, header=None, nrows=10)

    for i in range(min(10, len(df))):
        for j in range(min(15, len(df.columns))):
            cell = df.iloc[i, j]
            if pd.isna(cell):
                continue
            cell_str = str(cell)

            # Look for date in cell (MM/DD/YYYY format)
            match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", cell_str)
            if match:
                return f"{match.group(3)}-{match.group(1).zfill(2)}"

    # Fallback to current date
    return datetime.now().strftime("%Y-%m")


def append_to_history(
    records: list[dict[str, Any]],
    snapshot_date: str,
    history_path: Path,
    columns: list[str],
) -> None:
    """Append records to parquet history file with snapshot_date column."""
    records_with_date = [{**r, "snapshot_date": snapshot_date} for r in records]
    new_df = pd.DataFrame(records_with_date)

    all_columns = ["snapshot_date"] + columns
    for col in all_columns:
        if col not in new_df.columns:
            new_df[col] = None
    new_df = new_df[all_columns]
    new_df = new_df.replace("", None)

    history_path.parent.mkdir(parents=True, exist_ok=True)

    if history_path.exists():
        existing_df = pd.read_parquet(history_path)
        existing_df = existing_df[existing_df["snapshot_date"] != snapshot_date]
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
    else:
        combined_df = new_df

    sort_cols = ["snapshot_date"]
    if "unit_id" in combined_df.columns:
        sort_cols.append("unit_id")
    elif "PlanCode" in combined_df.columns:
        sort_cols.append("PlanCode")
    combined_df = combined_df.sort_values(sort_cols, ascending=[False, True])

    combined_df.to_parquet(history_path, index=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse rent roll with vertical charge layout to standardized CSV."
    )
    parser.add_argument("input", type=Path, nargs="?", help="Input Excel file")
    parser.add_argument(
        "--config", "-c",
        help="Property config name (e.g., 'tower_village')",
    )
    parser.add_argument(
        "--auto-detect", "-a",
        action="store_true",
        help="Auto-detect property config from file content",
    )
    parser.add_argument("--output", "-o", type=Path, help="Output directory")
    parser.add_argument(
        "--snapshot-date",
        help="Override snapshot date (e.g., '2026-04-09'). Use when multiple rent rolls "
             "fall in the same month and you want separate snapshots.",
    )
    parser.add_argument(
        "--summary", "-s",
        action="store_true",
        help="Generate floorplan summary CSV",
    )
    parser.add_argument(
        "--list-configs",
        action="store_true",
        help="List available property configurations",
    )
    args = parser.parse_args()

    if args.list_configs:
        print("Available property configurations:")
        for cfg_name in list_available_configs():
            try:
                cfg = load_config(cfg_name)
                desc = cfg.get("property_name", "")
                print(f"  {cfg_name}: {desc}")
            except Exception:
                print(f"  {cfg_name}: (error loading)")
        return

    if not args.input:
        parser.error("input file is required")

    if not args.input.exists():
        raise FileNotFoundError(f"Input file not found: {args.input}")

    # Load or detect config
    config: dict[str, Any] | None = None
    config_name: str | None = None

    if args.config:
        config = load_config(args.config)
        config_name = args.config
        print(f"Using config: {config_name} ({config.get('property_name', '')})")
    elif args.auto_detect:
        df = pd.read_excel(args.input, header=None, nrows=10)
        detected = detect_property_from_file(df)
        if detected:
            config = load_config(detected)
            config_name = detected
            print(f"Auto-detected config: {config_name} ({config.get('property_name', '')})")
        else:
            print("Warning: Could not auto-detect property config. Using defaults.")
    else:
        print("No config specified. Using default column positions and charge rules.")

    # Parse
    print(f"Parsing {args.input}...")
    records = parse_rent_roll(args.input, config)
    print(f"Parsed {len(records)} units")

    # Extract snapshot date (or use CLI override)
    snapshot_date = args.snapshot_date or extract_snapshot_date(args.input)
    print(f"Snapshot date: {snapshot_date}")

    # Determine output directory
    if args.output:
        output_dir = args.output
    else:
        output_dir = args.input.parent.parent / "clean"

    # 1. Write current (latest) files
    std_path = output_dir / "rent_roll_standardized.csv"
    write_csv(records, std_path, CANONICAL_COLUMNS)
    print(f"Wrote (current): {std_path}")

    # 2. Write snapshot files
    snapshot_dir = output_dir / "snapshots" / snapshot_date
    snapshot_std_path = snapshot_dir / "rent_roll_standardized.csv"
    write_csv(records, snapshot_std_path, CANONICAL_COLUMNS)
    print(f"Wrote (snapshot): {snapshot_std_path}")

    # 3. Append to history parquet
    history_path = output_dir / "history" / "rent_roll_history.parquet"
    append_to_history(records, snapshot_date, history_path, CANONICAL_COLUMNS)
    print(f"Updated history: {history_path}")

    if args.summary:
        summary = generate_floorplan_summary(records)

        summary_path = output_dir / "floorplan_summary.csv"
        write_csv(summary, summary_path, FLOORPLAN_SUMMARY_COLUMNS)
        print(f"Wrote (current): {summary_path}")

        snapshot_summary_path = snapshot_dir / "floorplan_summary.csv"
        write_csv(summary, snapshot_summary_path, FLOORPLAN_SUMMARY_COLUMNS)
        print(f"Wrote (snapshot): {snapshot_summary_path}")

        summary_history_path = output_dir / "history" / "floorplan_history.parquet"
        append_to_history(summary, snapshot_date, summary_history_path, FLOORPLAN_SUMMARY_COLUMNS)
        print(f"Updated history: {summary_history_path}")

        print(f"\n{len(summary)} unique floorplans")
        print("\nFloorplan Summary:")
        for s in summary:
            plan, units, bed = s["PlanCode"], s["Units"], s["BedType"]
            sqft, rent = s["SqFt"], s["AvgMarketRent"]
            print(f"  {plan}: {units} units, {bed}, {sqft}sf, ${rent}")


if __name__ == "__main__":
    main()
