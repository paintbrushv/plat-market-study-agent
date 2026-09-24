"""Parse ledger-style RentRollDetail Excel exports.

This layout uses a unit header row followed by multiple charge/credit rows for
the same unit. The reusable column-mapping standardizer cannot recover scheduled
rent and ancillary charges from this shape because those values live in
description rows rather than dedicated columns.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl

CANONICAL_COLUMNS = [
    "asset",
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

SUMMARY_COLUMNS = ["PlanCode", "BedType", "Units", "SqFt", "AvgMarketRent", "OccupiedUnits"]

UNIT_TYPE_DEFAULTS: dict[str, tuple[str, float, int]] = {
    "A1": ("1BR", 1.0, 681),
    "A1R": ("1BR", 1.0, 681),
    "A1 ADA": ("1BR", 1.0, 681),
    "B1": ("2BR", 2.0, 957),
    "B1R": ("2BR", 2.0, 957),
    "B2": ("3BR", 2.0, 1225),
    "B2R": ("3BR", 2.0, 1225),
}

UNIT_ID_RE = re.compile(r"^\d{2,4}-\d{2,4}[A-Z]?$", re.IGNORECASE)


@dataclass
class UnitRecord:
    asset: str
    unit_id: str
    floorplan_code: str
    market_rent: float
    resident_name: str
    lease_start: str = ""
    lease_end: str = ""
    charges: dict[str, float] = field(default_factory=dict)
    credits: dict[str, float] = field(default_factory=dict)


def _num(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    return float(cleaned) if cleaned else 0.0


def _date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


def _split_lease_dates(value: Any) -> tuple[str, str]:
    parts = str(value or "").split()
    if len(parts) >= 2:
        return _date(parts[0]), _date(parts[1])
    return "", ""


def _clean_plan(value: Any) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip()).upper()
    raw = re.sub(r",?\s*W/D\b", "", raw).strip()
    parts = raw.split()
    if len(parts) >= 2 and parts[0] == parts[1]:
        return parts[0]
    return raw


def _resident_display_name(value: Any) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    if raw.lower().startswith("vacant unit"):
        return ""
    # Strip common resident/account/date suffixes while preserving the actual name.
    return re.sub(r"\s+T\d+\s+\d{1,2}/\d{1,2}/\d{4}$", "", raw).strip()


def _status(name: Any, credits: dict[str, float]) -> str:
    raw = str(name or "").strip().lower()
    if raw.startswith("vacant unit"):
        return "Vacant"
    if raw.startswith("model") or "mode-model" in {k.lower() for k in credits}:
        return "Model"
    if any("down" in k.lower() for k in credits):
        return "Down"
    return "Occupied"


def _charge_bucket(description: str) -> str:
    code = description.split("-", 1)[0].strip().upper()
    if code in {"LTOR", "VAC", "MODE", "DOWN", "LTOL"}:
        return "ignore"
    if code == "RENT":
        return "base_rent"
    if code == "PETR":
        return "pet_rent"
    if code == "GAR":
        return "parking_rent"
    if code in {"STOR", "STG"}:
        return "storage_rent"
    if code in {"W/D", "VALT", "CPCR", "RLL", "MTOM", "STLF", "PETF"}:
        return "other_income"
    return "other_income"


def _credit_bucket(description: str) -> str:
    code = description.split("-", 1)[0].strip().upper()
    if code in {"VAC", "MODE", "DOWN", "LTOL", "LTOR"}:
        return "ignore"
    if code in {"CONC"}:
        return "concessions"
    return "ignore"


def parse_file(path: Path, *, asset: str = "") -> list[dict[str, Any]]:
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    worksheet = workbook["RentRollDetail"] if "RentRollDetail" in workbook.sheetnames else workbook[workbook.sheetnames[0]]

    units: list[UnitRecord] = []
    current: UnitRecord | None = None

    for row in worksheet.iter_rows(min_row=3, values_only=True):
        unit_id = str(row[0] or "").strip()
        description = str(row[8] or "").strip()
        if unit_id.lower().startswith("total"):
            break
        if unit_id:
            if not UNIT_ID_RE.match(unit_id):
                continue
            lease_start, lease_end = _split_lease_dates(row[4])
            current = UnitRecord(
                asset=asset,
                unit_id=unit_id,
                floorplan_code=_clean_plan(row[1]),
                market_rent=_num(row[2]),
                resident_name=str(row[3] or "").strip(),
                lease_start=lease_start,
                lease_end=lease_end,
            )
            units.append(current)
        if current is None or not description:
            continue
        charge = _num(row[9])
        credit = _num(row[10])
        if charge:
            bucket = _charge_bucket(description)
            if bucket != "ignore":
                current.charges[bucket] = current.charges.get(bucket, 0.0) + charge
        if credit:
            bucket = _credit_bucket(description)
            if bucket != "ignore":
                current.credits[bucket] = current.credits.get(bucket, 0.0) + credit

    records: list[dict[str, Any]] = []
    for unit in units:
        bed_type, bath_count, sqft = UNIT_TYPE_DEFAULTS.get(unit.floorplan_code, ("Unknown", None, None))
        base_rent = unit.charges.get("base_rent", 0.0)
        pet_rent = unit.charges.get("pet_rent", 0.0)
        parking_rent = unit.charges.get("parking_rent", 0.0)
        storage_rent = unit.charges.get("storage_rent", 0.0)
        other_income = unit.charges.get("other_income", 0.0)
        concessions = unit.credits.get("concessions", 0.0)
        status = _status(unit.resident_name, unit.credits)
        records.append(
            {
                "asset": unit.asset,
                "unit_id": unit.unit_id,
                "floorplan_code": unit.floorplan_code,
                "bed_type": bed_type,
                "bath_count": bath_count,
                "sqft": sqft,
                "market_rent": unit.market_rent,
                "lease_rent": 0.0 if status in {"Vacant", "Down"} else base_rent,
                "status": status,
                "resident_name": _resident_display_name(unit.resident_name),
                "move_in_date": "",
                "lease_start": unit.lease_start,
                "lease_end": unit.lease_end,
                "base_rent": 0.0 if status in {"Vacant", "Down"} else base_rent,
                "pet_rent": pet_rent,
                "parking_rent": parking_rent,
                "storage_rent": storage_rent,
                "utility_reimbursement": 0.0,
                "other_income": other_income,
                "concessions": concessions,
                "total_rent": base_rent + pet_rent + parking_rent + storage_rent + other_income,
            }
        )
    return records


def floorplan_summary(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(str(record["floorplan_code"]), []).append(record)

    summary: list[dict[str, Any]] = []
    for plan, rows in sorted(grouped.items()):
        market = [float(row["market_rent"]) for row in rows if row.get("market_rent") is not None]
        sqft = [float(row["sqft"]) for row in rows if row.get("sqft") is not None]
        summary.append(
            {
                "PlanCode": plan,
                "BedType": rows[0].get("bed_type", ""),
                "Units": len(rows),
                "SqFt": round(sum(sqft) / len(sqft), 0) if sqft else 0,
                "AvgMarketRent": round(sum(market) / len(market), 2) if market else 0,
                "OccupiedUnits": sum(1 for row in rows if row.get("status") == "Occupied"),
            }
        )
    return summary


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--asset", action="append", default=[], help="Asset label for each input file")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.asset and len(args.asset) != len(args.inputs):
        raise ValueError("--asset must be provided once per input, or not at all")

    records: list[dict[str, Any]] = []
    for index, input_path in enumerate(args.inputs):
        asset = args.asset[index] if args.asset else input_path.stem
        parsed = parse_file(input_path, asset=asset)
        print(f"{input_path}: parsed {len(parsed)} units")
        records.extend(parsed)

    _write_csv(args.output_dir / "rent_roll_standardized.csv", records, CANONICAL_COLUMNS)
    if args.summary:
        _write_csv(args.output_dir / "floorplan_summary.csv", floorplan_summary(records), SUMMARY_COLUMNS)


if __name__ == "__main__":
    main()
