"""Parse Yardi 'Rent Roll with Lease Charges' format.

This parser handles the multi-row Yardi format where each unit has a main row
followed by charge detail rows. Property-specific logic (floorplan patterns,
charge codes) is defined in YAML config files.

Usage:
    # With property config
    python etl/parse_yardi_lease_charges.py input.xlsx --config palencia --summary

    # Auto-detect property from file
    python etl/parse_yardi_lease_charges.py input.xlsx --auto-detect --summary
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

# Column indices for Yardi Rent Roll with Lease Charges format
COL_UNIT = 0
COL_UNIT_TYPE = 1
COL_SQFT = 2
COL_RESIDENT_ID = 3
COL_NAME = 4
COL_MARKET_RENT = 5
COL_CHARGE_CODE = 6
COL_AMOUNT = 7
COL_MOVE_IN = 10
COL_LEASE_EXP = 11

# Base charge code mappings (can be extended by config)
BASE_CHARGE_CODES: dict[str, set[str]] = {
    "rent": {"rent"},
    "pet": {"petrent", "petfee"},
    "parking": {"park", "parking", "carport"},
    "utility": {"trash", "adminrub", "admin", "pest", "water", "sewer", "elec"},
    "concession": {"concrenw", "conc", "concession"},
    "other": {"pckrm", "mtm", "empl", "stor", "storage"},
}

# Default skip patterns
DEFAULT_SKIP_PATTERNS = [
    "Rent Roll",
    "As Of",
    "Month Year",
    "Current/Notice",
    "Future Residents",
    "Applicants",
    "Total Non Rev",
    "Summary of Charges",
    "Charge Code",
    "Total",
    "Square",
]

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

CONFIGS_DIR = Path("configs/yardi_lease_charges")


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
    configs = []
    for path in CONFIGS_DIR.glob("*.yaml"):
        if not path.name.startswith("_"):
            configs.append(path.stem)
    return sorted(configs)


def detect_property_from_file(df: pd.DataFrame) -> str | None:
    """Try to detect which property config to use from file content."""
    # Look in first 10 rows for property identifiers
    for i in range(min(10, len(df))):
        for j in range(min(5, len(df.columns))):
            cell = df.iloc[i, j]
            if pd.isna(cell):
                continue
            cell_str = str(cell).lower()

            # Try to match against known property IDs
            for config_name in list_available_configs():
                try:
                    config = load_config(config_name)
                    prop_id = config.get("property_id", "").lower()
                    prop_name = config.get("property_name", "").lower()
                    if prop_id and prop_id in cell_str:
                        return config_name
                    if prop_name and prop_name in cell_str:
                        return config_name
                except Exception:
                    continue
    return None


def build_charge_code_sets(
    config: dict[str, Any] | None,
) -> tuple[set[str], set[str], set[str], set[str], set[str], set[str]]:
    """Build charge code sets from config, extending base codes."""
    rent_codes = BASE_CHARGE_CODES["rent"].copy()
    pet_codes = BASE_CHARGE_CODES["pet"].copy()
    parking_codes = BASE_CHARGE_CODES["parking"].copy()
    utility_codes = BASE_CHARGE_CODES["utility"].copy()
    concession_codes = BASE_CHARGE_CODES["concession"].copy()
    other_codes = BASE_CHARGE_CODES["other"].copy()

    if config and "charge_codes" in config:
        cc = config["charge_codes"]
        if "rent" in cc:
            rent_codes.update(cc["rent"])
        if "pet" in cc:
            pet_codes.update(cc["pet"])
        if "parking" in cc:
            parking_codes.update(cc["parking"])
        if "utility" in cc:
            utility_codes.update(cc["utility"])
        if "concession" in cc:
            concession_codes.update(cc["concession"])
        if "other" in cc:
            other_codes.update(cc["other"])

    return rent_codes, pet_codes, parking_codes, utility_codes, concession_codes, other_codes


def _apply_single_rule(floorplan_code: str, rule: dict[str, str], result_key: str) -> str | None:
    """Apply one floorplan rule and return the configured result if it matches."""
    match_type = rule.get("match_type", "prefix")
    pattern = rule.get("pattern", "")
    configured_result = rule.get(result_key, "")
    if not pattern or not configured_result:
        return None

    code_upper = floorplan_code.upper()
    code_clean = code_upper.replace("-PL", "")

    matched = False
    match: re.Match[str] | None = None
    if match_type == "prefix":
        matched = code_clean.startswith(pattern.upper())
    elif match_type == "suffix":
        matched = code_clean.endswith(pattern.upper())
    elif match_type == "contains":
        matched = pattern.upper() in code_clean
    elif match_type == "regex":
        match = re.search(pattern, code_clean, re.IGNORECASE)
        matched = match is not None

    if not matched:
        return None

    result = configured_result
    if match is not None:
        for i, group in enumerate(match.groups(), 1):
            if group:
                result = result.replace(f"{{{i}}}", group)
    return result


def apply_bed_type_rules(
    floorplan_code: str,
    rules: list[dict[str, str]],
    bath_rules: list[dict[str, str]] | None = None,
) -> tuple[str, str]:
    """Apply bed/bath rules from config to determine cohort fields."""
    if not floorplan_code:
        return "", ""

    bed_type = ""
    for rule in rules:
        matched = _apply_single_rule(floorplan_code, rule, "bed_type")
        if matched:
            bed_type = matched
            break

    bath_count = ""
    for rule in bath_rules or []:
        matched = _apply_single_rule(floorplan_code, rule, "bath_count")
        if matched:
            bath_count = matched
            break

    return bed_type, bath_count


def should_skip_unit(
    unit_str: str,
    skip_patterns: list[str],
    all_charge_codes: set[str],
    unit_id_patterns: list[str] | None = None,
) -> bool:
    """Determine if a unit ID should be skipped."""
    # Skip if matches any skip pattern
    unit_lower = unit_str.lower()
    for pattern in skip_patterns:
        if pattern.lower() in unit_lower:
            return True

    # Skip if it's a charge code that got parsed as unit ID
    if unit_lower in all_charge_codes:
        return True

    # If unit_id_patterns defined, skip if unit doesn't match any pattern
    if unit_id_patterns:
        matches_pattern = False
        for pattern in unit_id_patterns:
            if re.match(pattern, unit_str, re.IGNORECASE):
                matches_pattern = True
                break
        if not matches_pattern:
            return True

    return False


def _numeric_or_none(value: Any) -> float | None:
    """Return a float for spreadsheet numeric cells, otherwise None."""
    if pd.isna(value):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        text = str(value).replace(",", "").replace("$", "").strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _looks_like_physical_unit_row(row: pd.Series) -> bool:
    """Filter charge-code/summary rows that appear after unit sections.

    Yardi "Rent Roll with Lease Charges" reports can append a Summary of
    Charges table where the first column is a charge code. Without this guard,
    codes like UTRSH, GARAGE, and PETRNT can be counted as occupied units.
    """
    floorplan = row[COL_UNIT_TYPE]
    if pd.isna(floorplan) or not str(floorplan).strip():
        return False
    if _numeric_or_none(row[COL_SQFT]) is None:
        return False
    if _numeric_or_none(row[COL_MARKET_RENT]) is None:
        return False
    return True


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
    """Parse Yardi Rent Roll with Lease Charges format."""
    df = pd.read_excel(input_path, header=None)

    # Build charge code sets
    rent_codes, pet_codes, parking_codes, utility_codes, concession_codes, other_codes = (
        build_charge_code_sets(config)
    )
    all_charge_codes = (
        rent_codes | pet_codes | parking_codes | utility_codes | concession_codes | other_codes
    )

    # Get skip patterns from config or use defaults
    skip_patterns = DEFAULT_SKIP_PATTERNS.copy()
    if config:
        skip_patterns = config.get("skip_patterns", skip_patterns)

    # Get unit_id_patterns from config (for validating unit IDs)
    unit_id_patterns = config.get("unit_id_patterns", []) if config else []

    # Get bed type rules from config
    bed_type_rules = config.get("bed_type_rules", []) if config else []
    bath_rules = config.get("bath_rules", []) if config else []

    # Section stop markers - stop processing when we hit these sections
    stop_section_markers = ["future residents", "applicants", "total non rev", "summary"]

    units: dict[str, dict[str, Any]] = {}
    current_unit: str | None = None
    in_current_residents_section = False
    past_current_residents_section = False

    for _idx, row in df.iterrows():
        # Check if this row marks the start/end of a section
        first_cell = str(row[COL_UNIT]).lower() if not pd.isna(row[COL_UNIT]) else ""

        # Check if we're entering the Current/Notice/Vacant Residents section
        if "current/notice" in first_cell or "current residents" in first_cell:
            in_current_residents_section = True
            past_current_residents_section = False  # Reset for multi-property files
            continue

        # Check if we've hit a stop section (Future Residents, Totals, Summary)
        if in_current_residents_section:
            for marker in stop_section_markers:
                if marker in first_cell:
                    past_current_residents_section = True
                    break

        # Skip all rows after we've passed the current residents section
        if past_current_residents_section:
            continue
        unit_id = row[COL_UNIT]
        charge_code = row[COL_CHARGE_CODE]
        amount = row[COL_AMOUNT]

        # Check if this is a charge detail row (no unit ID)
        if pd.isna(unit_id):
            if current_unit and not pd.isna(charge_code) and str(charge_code) != "Total":
                code = str(charge_code).lower()

                # Parse amount safely
                try:
                    amt = float(amount) if not pd.isna(amount) else 0
                except (ValueError, TypeError):
                    continue

                if code in rent_codes:
                    units[current_unit]["base_rent"] = amt
                elif code in pet_codes:
                    units[current_unit]["pet_rent"] += amt
                elif code in parking_codes:
                    units[current_unit]["parking_rent"] += amt
                elif code in utility_codes:
                    units[current_unit]["utility_reimbursement"] += amt
                elif code in concession_codes:
                    units[current_unit]["concessions"] += amt
                elif code in other_codes:
                    units[current_unit]["other_income"] += amt
        else:
            unit_str = str(unit_id)

            # Skip non-unit rows
            if unit_str in ("Unit", "nan"):
                continue
            if should_skip_unit(unit_str, skip_patterns, all_charge_codes, unit_id_patterns):
                continue

            if not _looks_like_physical_unit_row(row):
                continue

            # Skip if market rent column has non-numeric value (header row)
            market_val = row[COL_MARKET_RENT]
            if not pd.isna(market_val) and not isinstance(market_val, int | float):
                try:
                    float(market_val)
                except (ValueError, TypeError):
                    continue

            current_unit = unit_str

            # Determine bed type using config rules or empty
            floorplan = str(row[COL_UNIT_TYPE]) if not pd.isna(row[COL_UNIT_TYPE]) else ""
            bed_type, bath = apply_bed_type_rules(floorplan, bed_type_rules, bath_rules)

            # Determine status from name
            name = str(row[COL_NAME]) if not pd.isna(row[COL_NAME]) else ""
            name_upper = name.upper()

            # Check for non-revenue units (model, show unit, down, etc.)
            non_rev_keywords = [
                "MODEL",
                "NON-REV",
                "NONREV",
                "NON REV",
                "SHOW UNIT",
                "SHOW APT",
                "DOWN",
                "OFFICE",
                "EMPLOYEE",
            ]

            if name_upper == "VACANT":
                status = "Vacant"
            elif any(keyword in name_upper for keyword in non_rev_keywords):
                status = "Non-Revenue"
            else:
                status = "Occupied"

            units[current_unit] = {
                "unit_id": current_unit,
                "floorplan_code": floorplan,
                "bed_type": bed_type,
                "bath_count": bath if bath else "",
                "sqft": int(row[COL_SQFT]) if not pd.isna(row[COL_SQFT]) else "",
                "market_rent": float(row[COL_MARKET_RENT])
                if not pd.isna(row[COL_MARKET_RENT])
                else "",
                "lease_rent": "",
                "status": status,
                "resident_name": name if status == "Occupied" else "",
                "move_in_date": format_date(row[COL_MOVE_IN]),
                "lease_start": "",
                "lease_end": format_date(row[COL_LEASE_EXP]),
                "base_rent": 0.0,
                "pet_rent": 0.0,
                "parking_rent": 0.0,
                "storage_rent": 0.0,
                "utility_reimbursement": 0.0,
                "other_income": 0.0,
                "concessions": 0.0,
                "total_rent": 0.0,
            }

            # First row may also have a charge code
            if not pd.isna(charge_code) and str(charge_code) != "Total":
                code = str(charge_code).lower()
                try:
                    amt = float(amount) if not pd.isna(amount) else 0
                except (ValueError, TypeError):
                    amt = 0

                if code in rent_codes:
                    units[current_unit]["base_rent"] = amt
                elif code in pet_codes:
                    units[current_unit]["pet_rent"] = amt
                elif code in parking_codes:
                    units[current_unit]["parking_rent"] = amt
                elif code in utility_codes:
                    units[current_unit]["utility_reimbursement"] = amt

    # Calculate total rent for each unit
    for u in units.values():
        total = (
            u["base_rent"]
            + u["pet_rent"]
            + u["parking_rent"]
            + u["storage_rent"]
            + u["utility_reimbursement"]
            + u["other_income"]
            - u["concessions"]
        )
        u["total_rent"] = round(total, 2)
        u["lease_rent"] = u["base_rent"] if u["base_rent"] > 0 else ""

    return list(units.values())


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

        summary.append(
            {
                "PlanCode": plan_code,
                "BedType": bed_type,
                "Units": len(plan_units),
                "SqFt": round(avg_sqft),
                "AvgMarketRent": round(avg_rent, 2),
            }
        )

    return summary


def write_csv(records: list[dict[str, Any]], output_path: Path, columns: list[str]) -> None:
    """Write records to CSV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def extract_snapshot_date(input_path: Path, df: pd.DataFrame | None = None) -> str:
    """Extract snapshot date from filename or file content.

    Tries to find date in:
    1. Filename (e.g., RentRoll_2025-11-26.xlsx or RentRoll11_26_2025.xlsx)
    2. File content "As Of" row (e.g., "As Of = 11/26/2025")
    3. Falls back to today's date
    """
    # Try filename first
    filename = input_path.stem
    # Pattern: YYYY-MM-DD or YYYY_MM_DD
    match = re.search(r"(\d{4})[-_](\d{2})[-_](\d{2})", filename)
    if match:
        return f"{match.group(1)}-{match.group(2)}"

    # Pattern: MM-DD-YYYY or MM_DD_YYYY
    match = re.search(r"(\d{2})[-_](\d{2})[-_](\d{4})", filename)
    if match:
        return f"{match.group(3)}-{match.group(1)}"

    # Pattern: MMDDYYYY
    match = re.search(r"(\d{2})(\d{2})(\d{4})", filename)
    if match:
        return f"{match.group(3)}-{match.group(1)}"

    # Try file content
    if df is None:
        df = pd.read_excel(input_path, header=None, nrows=10)

    for i in range(min(10, len(df))):
        for j in range(min(5, len(df.columns))):
            cell = df.iloc[i, j]
            if pd.isna(cell):
                continue
            cell_str = str(cell)
            # Look for "As Of = MM/DD/YYYY"
            match = re.search(r"As Of\s*=?\s*(\d{1,2})/(\d{1,2})/(\d{4})", cell_str)
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
    # Add snapshot_date to each record
    records_with_date = [{**r, "snapshot_date": snapshot_date} for r in records]
    new_df = pd.DataFrame(records_with_date)

    # Ensure columns are in correct order with snapshot_date first
    all_columns = ["snapshot_date"] + columns
    for col in all_columns:
        if col not in new_df.columns:
            new_df[col] = None
    new_df = new_df[all_columns]

    # Replace empty strings with None for parquet compatibility
    new_df = new_df.replace("", None)

    history_path.parent.mkdir(parents=True, exist_ok=True)

    if history_path.exists():
        # Load existing and append
        existing_df = pd.read_parquet(history_path)
        # Remove any existing records for this snapshot date (replace mode)
        existing_df = existing_df[existing_df["snapshot_date"] != snapshot_date]
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
    else:
        combined_df = new_df

    # Sort by snapshot_date descending, then by unit_id
    sort_cols = ["snapshot_date"]
    if "unit_id" in combined_df.columns:
        sort_cols.append("unit_id")
    elif "PlanCode" in combined_df.columns:
        sort_cols.append("PlanCode")
    combined_df = combined_df.sort_values(sort_cols, ascending=[False, True])

    combined_df.to_parquet(history_path, index=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse Yardi Rent Roll with Lease Charges to standardized CSV."
    )
    parser.add_argument("input", type=Path, nargs="?", help="Input Excel file")
    parser.add_argument(
        "--config",
        "-c",
        help="Property config name (e.g., 'palencia', 'example_m')",
    )
    parser.add_argument(
        "--auto-detect",
        "-a",
        action="store_true",
        help="Auto-detect property config from file content",
    )
    parser.add_argument("--output", "-o", type=Path, help="Output directory")
    parser.add_argument(
        "--summary",
        "-s",
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
            print("Warning: Could not auto-detect property config. Using base patterns only.")
    else:
        print("No config specified. Using base patterns only.")
        print("Hint: Use --config <name> or --auto-detect for property-specific parsing.")

    # Parse
    print(f"Parsing {args.input}...")
    records = parse_rent_roll(args.input, config)
    print(f"Parsed {len(records)} units")

    # Extract snapshot date from file
    snapshot_date = extract_snapshot_date(args.input)
    print(f"Snapshot date: {snapshot_date}")

    # Determine output directory
    # Default: {input_dir}/../clean/ (e.g., rent-roll/raw/ -> rent-roll/clean/)
    if args.output:
        output_dir = args.output
    else:
        # Go up from raw/ to rent-roll/, then into clean/
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

        # 1. Write current summary
        summary_path = output_dir / "floorplan_summary.csv"
        write_csv(summary, summary_path, FLOORPLAN_SUMMARY_COLUMNS)
        print(f"Wrote (current): {summary_path}")

        # 2. Write snapshot summary
        snapshot_summary_path = snapshot_dir / "floorplan_summary.csv"
        write_csv(summary, snapshot_summary_path, FLOORPLAN_SUMMARY_COLUMNS)
        print(f"Wrote (snapshot): {snapshot_summary_path}")

        # 3. Append to summary history
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
