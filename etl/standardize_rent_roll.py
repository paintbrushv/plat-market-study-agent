"""Standardize rent roll files to canonical CSV format.

This script transforms rent roll exports from various property management systems
(Yardi, RealPage, AppFolio, ResMan) into a standardized CSV format using
configurable column mappings.

Usage:
    python etl/standardize_rent_roll.py input.xlsx --config yardi --output clean.csv
    python etl/standardize_rent_roll.py input.xlsx --auto-detect --output clean.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

# Optional pyarrow for parquet support
pa: Any | None
try:
    import pyarrow as _pa
except ImportError:
    pa = None
else:
    pa = _pa

# Optional pandas for Excel support
pd: Any | None
try:
    import pandas as _pd
except ImportError:
    pd = None
else:
    pd = _pd

# Optional openpyxl for xlsx support
openpyxl: Any | None
try:
    import openpyxl as _openpyxl
except ImportError:
    openpyxl = None
else:
    openpyxl = _openpyxl

CONFIGS_DIR = Path("configs/rent_roll_mappings")

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

# Columns for floorplan summary output
FLOORPLAN_SUMMARY_COLUMNS = [
    "PlanCode",
    "BedType",
    "Units",
    "SqFt",
    "AvgMarketRent",
]


def load_config(config_name: str) -> dict[str, Any]:
    """Load a mapping configuration by name."""
    config_path = CONFIGS_DIR / f"{config_name}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    return cast(dict[str, Any], yaml.safe_load(config_path.read_text(encoding="utf-8")))


def list_available_configs() -> list[str]:
    """List all available mapping configurations."""
    configs = []
    for path in CONFIGS_DIR.glob("*.yaml"):
        if not path.name.startswith("_"):
            configs.append(path.stem)
    return sorted(configs)


def detect_config(headers: list[str]) -> str | None:
    """Auto-detect which config to use based on column headers."""
    headers_lower = {h.lower().strip() for h in headers if h}

    # Detection heuristics based on unique column patterns
    detection_rules = [
        ("yardi", {"unitcode", "floorplan", "marketrent"}),
        ("yardi", {"unit code", "floor plan", "market rent"}),
        ("realpage", {"apt", "floor plan", "actual rent"}),
        ("realpage", {"apt #", "sq. ft.", "asking rent"}),
        ("appfolio", {"unit name", "target rent", "tenant name"}),
        ("appfolio", {"listed rent", "move-in date", "lease end date"}),
        ("resman", {"floor plan name", "scheduled rent", "lease expiration"}),
        ("resman", {"fp code", "advertised rent", "occupancy status"}),
    ]

    for config_name, keywords in detection_rules:
        if keywords.issubset(headers_lower):
            return config_name

    # Fallback: try each config and score by matched columns
    best_config = None
    best_score = 0

    for config_name in list_available_configs():
        try:
            config = load_config(config_name)
            score = 0
            for _field, aliases in config.get("column_mappings", {}).items():
                for alias in aliases:
                    if alias.lower() in headers_lower:
                        score += 1
                        break
            if score > best_score:
                best_score = score
                best_config = config_name
        except Exception:
            continue

    return best_config if best_score >= 3 else None


def read_input_file(
    input_path: Path, config: dict[str, Any]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Read input file and return headers and rows."""
    file_settings = config.get("file_settings", {})
    extension = input_path.suffix.lower()

    if extension == ".csv":
        return _read_csv(input_path, file_settings)
    elif extension in (".xlsx", ".xls"):
        return _read_excel(input_path, file_settings)
    else:
        raise ValueError(f"Unsupported file format: {extension}")


def _read_csv(path: Path, settings: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Read CSV file."""
    encoding = settings.get("encoding", "utf-8")
    header_row = settings.get("header_row", 0)

    with path.open("r", encoding=encoding, errors="replace") as f:
        lines = list(csv.reader(f))

    if not lines:
        return [], []

    # Auto-detect header row if needed
    if header_row == "auto":
        header_row = _detect_header_row(lines, settings.get("detect_header_keywords", ["Unit"]))

    headers = [str(h).strip() for h in lines[header_row]]
    rows = []
    for line in lines[header_row + 1 :]:
        if len(line) >= len(headers):
            row = {headers[i]: line[i] for i in range(len(headers))}
            rows.append(row)
        elif line:  # Partial row
            row = {headers[i]: line[i] if i < len(line) else "" for i in range(len(headers))}
            rows.append(row)

    return headers, rows


def _read_excel(path: Path, settings: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Read Excel file."""
    if pd is None:
        raise ImportError("pandas is required for Excel file support")

    sheet = settings.get("sheet", 0)
    header_row = settings.get("header_row", 0)

    # Read without header first if auto-detecting
    if header_row == "auto":
        df_raw = pd.read_excel(path, sheet_name=sheet, header=None)
        raw_rows = df_raw.values.tolist()
        header_row = _detect_header_row(raw_rows, settings.get("detect_header_keywords", ["Unit"]))

    df = pd.read_excel(path, sheet_name=sheet, header=header_row)
    df = df.fillna("")
    skip_after_header = int(settings.get("skip_rows_after_header", 0) or 0)
    if skip_after_header:
        df = df.iloc[skip_after_header:].reset_index(drop=True)

    headers = [str(h).strip() for h in df.columns.tolist()]
    rows = df.to_dict("records")

    return headers, rows


def _detect_header_row(rows: list[list[Any]], keywords: list[str]) -> int:
    """Detect which row contains headers based on keywords."""
    keywords_lower = {k.lower() for k in keywords}

    for i, row in enumerate(rows[:20]):  # Check first 20 rows
        row_values = {str(v).lower().strip() for v in row if v}
        if row_values & keywords_lower:
            return i

    return 0  # Default to first row


def find_column(
    headers: list[str], aliases: list[str], headers_map: dict[str, int] | None = None
) -> int | None:
    """Find column index matching any of the aliases."""
    if headers_map is None:
        headers_map = {h.lower().strip(): i for i, h in enumerate(headers)}

    for alias in aliases:
        alias_lower = alias.lower().strip()
        if alias_lower in headers_map:
            return headers_map[alias_lower]

    return None


def get_value(row: dict[str, Any], headers: list[str], col_idx: int | None) -> str:
    """Get value from row by column index."""
    if col_idx is None:
        return ""
    header = headers[col_idx]
    value = row.get(header, "")
    return str(value).strip() if value is not None else ""


def clean_currency(value: str) -> float | None:
    """Extract numeric value from currency string."""
    if not value:
        return None
    # Remove currency symbols, commas, spaces
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def clean_sqft(value: str) -> int | None:
    """Extract numeric value from square footage string."""
    if not value:
        return None
    # Remove non-numeric except decimal
    cleaned = re.sub(r"[^\d.]", "", str(value))
    try:
        return int(float(cleaned)) if cleaned else None
    except ValueError:
        return None


def normalize_status(value: str, mappings: dict[str, list[str]]) -> str:
    """Normalize status value using mappings."""
    if not value:
        return ""
    value_lower = value.lower().strip()

    for canonical, aliases in mappings.items():
        for alias in aliases:
            if alias.lower() == value_lower:
                return canonical

    return value  # Return original if no match


def normalize_bed_type(
    value: str, mappings: dict[str, list[str]], *, allow_numeric_fallback: bool = True
) -> str:
    """Normalize bed type value using mappings."""
    if not value:
        return ""
    value_str = str(value).strip()
    value_lower = value_str.lower()

    for canonical, aliases in mappings.items():
        for alias in aliases:
            if alias.lower() == value_lower:
                return canonical

    # Try numeric extraction for true bed-count fields, but avoid applying it
    # to opaque plan codes like "A4 Alt 2" where the digit is only a variant.
    if allow_numeric_fallback:
        match = re.search(r"(\d+)", value_str)
        if match:
            num = int(match.group(1))
            if num == 0:
                return "Studio"
            elif num <= 4:
                return f"{num}BR"

    return value_str


def infer_bed_type_from_floorplan(
    floorplan_code: str,
    prefix_map: dict[str, str],
) -> str:
    """Infer bed type from configured floorplan prefixes."""
    if not floorplan_code:
        return ""
    code_upper = str(floorplan_code).strip().upper()
    for prefix, bed_type in sorted(prefix_map.items(), key=lambda item: len(item[0]), reverse=True):
        if code_upper.startswith(str(prefix).upper()):
            return str(bed_type)
    return ""


def parse_date(value: str, formats: list[str]) -> str:
    """Parse date string and return ISO format."""
    if not value:
        return ""

    for fmt in formats:
        try:
            dt = datetime.strptime(str(value).strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return str(value).strip()  # Return original if no format matches


def parse_bed_bath_combined(value: str, separator: str = "/") -> tuple[str | None, float | None]:
    """Parse combined bed/bath field like '2/2' or '2BR/2BA'."""
    if not value or separator not in str(value):
        return None, None

    parts = str(value).split(separator)
    if len(parts) != 2:
        return None, None

    bed_str = re.sub(r"[^\d]", "", parts[0])
    bath_str = re.sub(r"[^\d.]", "", parts[1])

    beds = bed_str if bed_str else None
    baths = float(bath_str) if bath_str else None

    return beds, baths


def should_skip_row(
    row: dict[str, Any],
    unit_id: str,
    status: str,
    filters: dict[str, Any],
) -> bool:
    """Determine if row should be skipped based on filters."""
    # Check required non-empty fields
    if not unit_id:
        return True

    # Check skip patterns
    for pattern in filters.get("skip_patterns", []):
        if re.match(pattern, unit_id, re.IGNORECASE):
            return True

    # Check status filter
    include_statuses = filters.get("include_statuses", [])
    if include_statuses and status and status not in include_statuses:
        return True

    return False


def _derive_status_from_row(
    *,
    status: str,
    resident_name: str,
    lease_rent: float | None,
) -> str:
    """Infer status for PMS exports that encode occupancy in resident/rent fields."""
    if status:
        return status
    name = str(resident_name or "").strip().lower()
    if name in {"vacant", "vacancy"}:
        return "Vacant"
    if name == "model":
        return "Non-Revenue"
    if lease_rent is not None and lease_rent <= 0:
        return "Vacant"
    if resident_name:
        return "Occupied"
    return ""


def _apply_unit_type_mapping(
    *,
    floorplan_code: str,
    bed_type: str,
    bath_count: float | None,
    sqft: int | None,
    config: dict[str, Any],
) -> tuple[str, float | None, int | None]:
    mappings = config.get("unit_type_mappings", {}) or config.get("floorplan_mappings", {})
    mapped = mappings.get(str(floorplan_code).strip())
    if mapped:
        bed_type = str(mapped.get("bed_type") or bed_type)
        bath_count = float(mapped["bath_count"]) if mapped.get("bath_count") is not None else bath_count
        mapped_sqft = mapped.get("expected_sqft", mapped.get("sqft"))
        sqft = int(mapped_sqft) if mapped_sqft is not None else sqft

    if not bed_type or bath_count is None:
        for rule in config.get("floorplan_bed_bath_patterns", []):
            pattern = rule.get("pattern")
            if pattern and re.search(pattern, str(floorplan_code), re.IGNORECASE):
                bed_type = str(rule.get("bed_type") or bed_type)
                bath_count = float(rule["bath_count"]) if rule.get("bath_count") is not None else bath_count
                break

    return bed_type, bath_count, sqft


def transform_row(
    row: dict[str, Any],
    headers: list[str],
    config: dict[str, Any],
    headers_map: dict[str, int],
) -> dict[str, Any] | None:
    """Transform a single row using the config mappings."""
    col_mappings = config.get("column_mappings", {})
    income_mappings = config.get("income_columns", {})
    status_mappings = config.get("status_mappings", {})
    bed_mappings = config.get("bed_type_mappings", {})
    transforms = config.get("transformations", {})
    filters = config.get("row_filters", {})

    # Build column index cache
    def get_col(field: str, mappings: dict[str, Any] | None = None) -> int | None:
        effective_mappings = mappings if mappings is not None else col_mappings
        aliases = effective_mappings.get(field, [])
        return find_column(headers, aliases, headers_map)

    # Extract core fields
    unit_id = get_value(row, headers, get_col("unit_id"))
    floorplan_code = get_value(row, headers, get_col("floorplan_code"))
    status_raw = get_value(row, headers, get_col("status"))
    status = normalize_status(status_raw, status_mappings)

    # Handle bed/bath - check for combined field first
    bed_raw = get_value(row, headers, get_col("bed_type"))
    bath_raw = get_value(row, headers, get_col("bath_count"))

    if transforms.get("parse_bed_bath_combined") and not bath_raw:
        separator = transforms.get("bed_bath_separator", "/")
        parsed_bed, parsed_bath = parse_bed_bath_combined(bed_raw, separator)
        if parsed_bed is not None:
            bed_raw = parsed_bed
        if parsed_bath is not None:
            bath_raw = str(parsed_bath)

    infer_from_floorplan = bool(transforms.get("infer_bed_type_from_floorplan"))
    bed_type = normalize_bed_type(
        bed_raw,
        bed_mappings,
        allow_numeric_fallback=not infer_from_floorplan,
    )
    if infer_from_floorplan and bed_type == str(bed_raw).strip():
        inferred_bed_type = infer_bed_type_from_floorplan(
            floorplan_code,
            transforms.get("floorplan_bed_prefix_map", {}),
        )
        if inferred_bed_type:
            bed_type = inferred_bed_type

    try:
        bath_count = float(re.sub(r"[^\d.]", "", str(bath_raw))) if bath_raw else None
    except ValueError:
        bath_count = None

    # Square footage
    sqft_raw = get_value(row, headers, get_col("sqft"))
    sqft = clean_sqft(sqft_raw) if transforms.get("clean_sqft", True) else sqft_raw

    # Rents
    market_rent_raw = get_value(row, headers, get_col("market_rent"))
    lease_rent_raw = get_value(row, headers, get_col("lease_rent"))

    market_rent: float | None
    lease_rent: float | None
    if transforms.get("clean_currency", True):
        market_rent = clean_currency(market_rent_raw)
        lease_rent = clean_currency(lease_rent_raw)
    else:
        market_rent = float(market_rent_raw) if market_rent_raw else None
        lease_rent = float(lease_rent_raw) if lease_rent_raw else None

    resident_name = get_value(row, headers, get_col("resident_name"))
    if transforms.get("derive_status_from_name"):
        status = _derive_status_from_row(
            status=status,
            resident_name=resident_name,
            lease_rent=lease_rent,
        )

    bed_type, bath_count, sqft = _apply_unit_type_mapping(
        floorplan_code=floorplan_code,
        bed_type=bed_type,
        bath_count=bath_count,
        sqft=sqft if isinstance(sqft, int) else None,
        config=config,
    )

    # Skip if filtered after derived status is available.
    if should_skip_row(row, unit_id, status, filters):
        return None

    # Dates
    date_formats = [transforms.get("date_format", "%m/%d/%Y")]
    date_formats.extend(transforms.get("alternate_date_formats", []))

    move_in_raw = get_value(row, headers, get_col("move_in_date"))
    lease_start_raw = get_value(row, headers, get_col("lease_start"))
    lease_end_raw = get_value(row, headers, get_col("lease_end"))

    move_in_date = parse_date(move_in_raw, date_formats)
    lease_start = parse_date(lease_start_raw, date_formats)
    lease_end = parse_date(lease_end_raw, date_formats)

    # Income breakdown
    def get_income(field: str) -> float | None:
        col_idx = get_col(field, income_mappings)
        if col_idx is None:
            return None
        val = get_value(row, headers, col_idx)
        return clean_currency(val)

    base_rent = get_income("base_rent")
    pet_rent = get_income("pet_rent")
    parking_rent = get_income("parking_rent")
    storage_rent = get_income("storage_rent")
    utility_reimbursement = get_income("utility_reimbursement")
    other_income = get_income("other_income")
    concessions = get_income("concessions")

    # Calculate total rent if configured
    total_rent = lease_rent
    if transforms.get("calculate_total_rent") and base_rent is not None:
        income_items = [
            base_rent or 0,
            pet_rent or 0,
            parking_rent or 0,
            storage_rent or 0,
            utility_reimbursement or 0,
            other_income or 0,
        ]
        concession_val = concessions or 0
        # Concessions are typically negative, but stored as positive
        if concession_val > 0:
            concession_val = -concession_val
        total_rent = sum(income_items) + concession_val

    return {
        "unit_id": unit_id,
        "floorplan_code": floorplan_code,
        "bed_type": bed_type,
        "bath_count": bath_count,
        "sqft": sqft,
        "market_rent": market_rent,
        "lease_rent": lease_rent,
        "status": status,
        "resident_name": resident_name,
        "move_in_date": move_in_date,
        "lease_start": lease_start,
        "lease_end": lease_end,
        "base_rent": base_rent,
        "pet_rent": pet_rent,
        "parking_rent": parking_rent,
        "storage_rent": storage_rent,
        "utility_reimbursement": utility_reimbursement,
        "other_income": other_income,
        "concessions": concessions,
        "total_rent": total_rent,
    }


def transform_rent_roll(
    input_path: Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Transform entire rent roll file."""
    headers, rows = read_input_file(input_path, config)

    if not headers or not rows:
        return []

    # Build headers lookup map
    headers_map = {h.lower().strip(): i for i, h in enumerate(headers)}

    transformed = []
    for row in rows:
        first_nonempty = next(
            (str(v).strip() for v in row.values() if str(v).strip()),
            "",
        )
        for pattern in config.get("row_filters", {}).get("section_terminator_patterns", []):
            if re.match(pattern, first_nonempty, re.IGNORECASE):
                return transformed
        result = transform_row(row, headers, config, headers_map)
        if result is not None:
            transformed.append(result)

    return _dedupe_physical_units(transformed)


def _row_status_priority(record: dict[str, Any]) -> tuple[int, int, int, float]:
    """Rank duplicate unit rows, keeping current physical-unit rows first."""
    status = str(record.get("status") or "").strip().lower()
    if status == "occupied":
        status_score = 5
    elif status == "notice":
        status_score = 4
    elif status == "model":
        status_score = 3
    elif status in {"vacant", "vacant-leased", "vacant leased"}:
        status_score = 2
    else:
        status_score = 1

    has_move_in = 1 if record.get("move_in_date") else 0
    no_future_lease_marker = 1 if not record.get("lease_start") else 0
    total_rent = record.get("total_rent") or record.get("lease_rent") or 0
    try:
        rent_score = float(total_rent)
    except (TypeError, ValueError):
        rent_score = 0.0
    return status_score, has_move_in, no_future_lease_marker, rent_score


def _dedupe_physical_units(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate future/application rows for the same physical unit."""
    by_unit: dict[str, dict[str, Any]] = {}
    ordered_units: list[str] = []
    for record in records:
        unit_id = str(record.get("unit_id") or "").strip()
        if not unit_id:
            continue
        if unit_id not in by_unit:
            ordered_units.append(unit_id)
            by_unit[unit_id] = record
            continue
        if _row_status_priority(record) > _row_status_priority(by_unit[unit_id]):
            by_unit[unit_id] = record
    return [by_unit[unit_id] for unit_id in ordered_units]


def generate_floorplan_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate records into floorplan summary format."""
    # Group by floorplan
    by_plan: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        plan = rec.get("floorplan_code") or "Unknown"
        by_plan.setdefault(plan, []).append(rec)

    summary = []
    for plan_code, units in sorted(by_plan.items()):
        # Get bed type (should be consistent within plan)
        bed_types = {u.get("bed_type") for u in units if u.get("bed_type")}
        bed_type = (
            bed_types.pop() if len(bed_types) == 1 else (bed_types.pop() if bed_types else "")
        )

        # Count units
        unit_count = len(units)

        # Average sqft
        sqfts: list[float] = [
            float(sqft_val) for u in units if (sqft_val := u.get("sqft")) is not None
        ]
        avg_sqft = sum(sqfts) / len(sqfts) if sqfts else 0.0

        # Average market rent
        rents: list[float] = [
            float(rent_val) for u in units if (rent_val := u.get("market_rent")) is not None
        ]
        avg_rent = sum(rents) / len(rents) if rents else 0.0

        summary.append(
            {
                "PlanCode": plan_code,
                "BedType": bed_type,
                "Units": unit_count,
                "SqFt": round(avg_sqft, 0),
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


def write_json(records: list[dict[str, Any]], output_path: Path) -> None:
    """Write records to JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")


def extract_snapshot_date(input_path: Path) -> str:
    """Extract snapshot date (YYYY-MM) from filename or file content.

    Tries to find date in:
    1. Filename (e.g., RentRoll_2025-11-26.xlsx or RentRoll11_26_2025.xlsx)
    2. File content "As Of" row
    3. Falls back to today's date
    """
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

    # Try file content for Excel files
    if pd is not None and input_path.suffix.lower() in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(input_path, header=None, nrows=10)
            for i in range(min(10, len(df))):
                for j in range(min(5, len(df.columns))):
                    cell = df.iloc[i, j]
                    if pd.isna(cell):
                        continue
                    cell_str = str(cell)
                    match = re.search(
                        r"As Of\s*=?\s*(\d{1,2})/(\d{1,2})/(\d{4})", cell_str
                    )
                    if match:
                        return f"{match.group(3)}-{match.group(1).zfill(2)}"
        except Exception:
            pass

    # Fallback to current date
    return datetime.now().strftime("%Y-%m")


def append_to_history(
    records: list[dict[str, Any]],
    snapshot_date: str,
    history_path: Path,
    columns: list[str],
) -> None:
    """Append records to parquet history file with snapshot_date column.

    Uses upsert logic: replaces any existing records for the same snapshot_date.
    """
    if pd is None:
        print(f"  Warning: pandas not available, skipping history write to {history_path}")
        return

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
        existing_df = pd.read_parquet(history_path)
        # Remove existing records for this snapshot date (upsert)
        existing_df = existing_df[existing_df["snapshot_date"] != snapshot_date]
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
    else:
        combined_df = new_df

    # Sort by snapshot_date descending, then by unit_id or PlanCode
    sort_cols = ["snapshot_date"]
    if "unit_id" in combined_df.columns:
        sort_cols.append("unit_id")
    elif "PlanCode" in combined_df.columns:
        sort_cols.append("PlanCode")
    combined_df = combined_df.sort_values(sort_cols, ascending=[False, True])

    combined_df.to_parquet(history_path, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standardize rent roll files to canonical CSV format."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input rent roll file (CSV, XLSX, XLS)",
    )
    parser.add_argument(
        "--config",
        "-c",
        help="Mapping config name (yardi, realpage, appfolio, resman) or path to YAML",
    )
    parser.add_argument(
        "--auto-detect",
        "-a",
        action="store_true",
        help="Auto-detect config based on column headers",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output CSV path (default: input_standardized.csv)",
    )
    parser.add_argument(
        "--summary",
        "-s",
        action="store_true",
        help="Also generate floorplan summary CSV",
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        help="Floorplan summary output path (default: input_floorplan_summary.csv)",
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="Also output JSON format",
    )
    parser.add_argument(
        "--list-configs",
        action="store_true",
        help="List available mapping configurations",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (clean/). Enables snapshots and parquet history.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate without writing output",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.list_configs:
        print("Available mapping configurations:")
        for cfg_name in list_available_configs():
            cfg = load_config(cfg_name)
            desc = cfg.get("description", "")
            print(f"  {cfg_name}: {desc}")
        return

    if not args.input.exists():
        raise FileNotFoundError(f"Input file not found: {args.input}")

    # Determine config to use
    config: dict[str, Any]
    config_name: str

    if args.config:
        # Check if it's a path or a name
        config_path = Path(args.config)
        if config_path.exists():
            config = cast(dict[str, Any], yaml.safe_load(config_path.read_text(encoding="utf-8")))
            config_name = config_path.stem
        else:
            config = load_config(args.config)
            config_name = args.config
    elif args.auto_detect:
        # Read headers for detection
        temp_config: dict[str, Any] = {"file_settings": {"header_row": "auto"}}
        headers, _ = read_input_file(args.input, temp_config)
        detected = detect_config(headers)
        if detected is None:
            raise ValueError("Could not auto-detect config. Please specify --config explicitly.")
        print(f"Auto-detected config: {detected}")
        config = load_config(detected)
        config_name = detected
    else:
        raise ValueError("Must specify --config or --auto-detect")

    # Transform
    print(f"Transforming {args.input} using {config_name} config...")
    records = transform_rent_roll(args.input, config)
    print(f"Transformed {len(records)} unit records")

    if args.dry_run:
        print("\nSample output (first 5 records):")
        for rec in records[:5]:
            print(
                f"  {rec.get('unit_id')}: {rec.get('floorplan_code')} - "
                f"{rec.get('bed_type')} - ${rec.get('market_rent')}"
            )
        return

    # Extract snapshot date for history tracking
    snapshot_date = extract_snapshot_date(args.input)
    print(f"Snapshot date: {snapshot_date}")

    # Determine output directory and paths
    output_dir = args.output_dir
    if output_dir:
        output_path = output_dir / "rent_roll_standardized.csv"
    else:
        output_path = args.output or args.input.with_name(f"{args.input.stem}_standardized.csv")
        # Infer output_dir from output_path for snapshot/history writes
        output_dir = output_path.parent

    # 1. Write current (latest) standardized CSV
    write_csv(records, output_path, CANONICAL_COLUMNS)
    print(f"Wrote (current): {output_path}")

    # 2. Write snapshot files
    snapshot_dir = output_dir / "snapshots" / snapshot_date
    snapshot_std_path = snapshot_dir / "rent_roll_standardized.csv"
    write_csv(records, snapshot_std_path, CANONICAL_COLUMNS)
    print(f"Wrote (snapshot): {snapshot_std_path}")

    # 3. Append to history parquet
    history_path = output_dir / "history" / "rent_roll_history.parquet"
    append_to_history(records, snapshot_date, history_path, CANONICAL_COLUMNS)
    print(f"Updated history: {history_path}")

    if args.json:
        json_path = output_path.with_suffix(".json")
        write_json(records, json_path)
        print(f"Wrote JSON: {json_path}")

    if args.summary:
        summary = generate_floorplan_summary(records)

        # 1. Write current summary
        if output_dir and not args.summary_output:
            summary_path = output_dir / "floorplan_summary.csv"
        else:
            summary_path = args.summary_output or args.input.with_name(
                f"{args.input.stem}_floorplan_summary.csv"
            )
        write_csv(summary, summary_path, FLOORPLAN_SUMMARY_COLUMNS)
        print(f"Wrote (current): {summary_path}")

        # 2. Write snapshot summary
        snapshot_summary_path = snapshot_dir / "floorplan_summary.csv"
        write_csv(summary, snapshot_summary_path, FLOORPLAN_SUMMARY_COLUMNS)
        print(f"Wrote (snapshot): {snapshot_summary_path}")

        # 3. Append to summary history parquet
        summary_history_path = output_dir / "history" / "floorplan_history.parquet"
        append_to_history(
            summary, snapshot_date, summary_history_path, FLOORPLAN_SUMMARY_COLUMNS
        )
        print(f"Updated history: {summary_history_path}")

        print(f"\n{len(summary)} unique floorplans")


if __name__ == "__main__":
    main()
