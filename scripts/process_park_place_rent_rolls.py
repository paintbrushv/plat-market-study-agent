"""Process Park Place Apartments rent rolls (3 periods)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def parse_resman_multirow(file_path: str) -> pd.DataFrame:
    """Parse ResMan multi-row rent roll format (Nov 2025, Jan 2026)."""
    df = pd.read_excel(file_path, header=None)

    units: list[dict[str, Any]] = []
    current_unit: dict[str, Any] | None = None

    for _, row in df.iterrows():
        # Check if this is a unit header row (has unit number in col 0 or 1)
        unit_val = row.iloc[0] if pd.notna(row.iloc[0]) else row.iloc[1]

        # Unit numbers are 101-112 or 201-209
        if pd.notna(unit_val):
            try:
                unit_num = int(float(unit_val))
                if 100 <= unit_num <= 300:
                    # Save previous unit if exists
                    if current_unit is not None:
                        units.append(current_unit)

                    # Start new unit
                    current_unit = {
                        "unit_id": str(unit_num),
                        "floorplan_code": str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else "",
                        "sqft": int(float(row.iloc[4])) if pd.notna(row.iloc[4]) else 800,
                        "resident_name": str(row.iloc[5]) if pd.notna(row.iloc[5]) else "",
                        "status_code": str(row.iloc[11]).strip() if pd.notna(row.iloc[11]) else "V",
                        "market_rent": float(row.iloc[13]) if pd.notna(row.iloc[13]) else 0,
                        "base_rent": 0,
                        "amenity_fee": 0,
                        "pet_rent": 0,
                        "washer_dryer": 0,
                        "concessions": 0,
                        "other_income": 0,
                        "total_rent": 0,
                        "move_in_date": row.iloc[27]
                        if len(row) > 27 and pd.notna(row.iloc[27])
                        else None,
                        "lease_start": row.iloc[29]
                        if len(row) > 29 and pd.notna(row.iloc[29])
                        else None,
                        "lease_end": row.iloc[30]
                        if len(row) > 30 and pd.notna(row.iloc[30])
                        else None,
                    }

                    # First charge row is on the same line
                    desc = str(row.iloc[20]).lower() if pd.notna(row.iloc[20]) else ""
                    amount = float(row.iloc[23]) if pd.notna(row.iloc[23]) else 0
                    _assign_charge(current_unit, desc, amount)
                    continue
            except (ValueError, TypeError):
                pass

        # Check if this is a charge row for current unit
        if current_unit is not None:
            desc = str(row.iloc[20]).lower() if pd.notna(row.iloc[20]) else ""
            amount_val = row.iloc[23]
            if pd.notna(amount_val) and desc:
                try:
                    amount = float(amount_val)
                    _assign_charge(current_unit, desc, amount)
                except (ValueError, TypeError):
                    pass

    # Don't forget last unit
    if current_unit is not None:
        units.append(current_unit)

    return _build_dataframe(units)


def _assign_charge(unit: dict[str, Any], desc: str, amount: float) -> None:
    """Assign a charge amount to the appropriate category."""
    desc = desc.lower()
    if desc in ("rent", "lease rent"):
        unit["base_rent"] = amount
    elif "amenity" in desc:
        unit["amenity_fee"] = amount
    elif "pet" in desc:
        unit["pet_rent"] += amount
    elif "washer" in desc or "dryer" in desc or "w/d" in desc:
        unit["washer_dryer"] = amount
    elif "concession" in desc:
        unit["concessions"] = amount
    elif "total" in desc:
        unit["total_rent"] = amount
    elif "credit builder" in desc:
        unit["other_income"] += amount
    else:
        unit["other_income"] += amount


def parse_standard_format(file_path: str) -> pd.DataFrame:
    """Parse standard one-row-per-unit format (May 2025)."""
    df = pd.read_excel(file_path, sheet_name="Rent Roll", header=2)

    units: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        unit_val = row.get("Unit No.")
        if pd.isna(unit_val):
            continue
        try:
            unit_num = int(float(unit_val))
            if not (100 <= unit_num <= 300):
                continue
        except (ValueError, TypeError):
            continue

        status = str(row.get("Occupancy Status", "")).strip()
        status_code = "C" if status.lower() == "occupied" else "V"

        contract_rent = float(row.get("Contractual Rent", 0) or 0)
        other_income = float(row.get("Other Income", 0) or 0)

        units.append(
            {
                "unit_id": str(unit_num),
                "floorplan_code": str(row.get("Floor Plan", "")).strip(),
                "sqft": int(float(row.get("Net Sf", 800) or 800)),
                "resident_name": "",  # Not in this format
                "status_code": status_code,
                "market_rent": float(row.get("Market Rent", 0) or 0),
                "base_rent": contract_rent,
                "amenity_fee": 0,  # Included in Other Income
                "pet_rent": 0,
                "washer_dryer": 0,
                "concessions": float(row.get("Recurring Concessions", 0) or 0),
                "other_income": other_income,
                "total_rent": contract_rent + other_income,
                "move_in_date": row.get("Move In Date"),
                "lease_start": row.get("Lease Start Date"),
                "lease_end": row.get("Lease Expiration"),
            }
        )

    return _build_dataframe(units)


def _build_dataframe(units: list[dict[str, Any]]) -> pd.DataFrame:
    """Build standardized DataFrame from units list."""
    df = pd.DataFrame(units)

    # Derive bed type from floorplan
    df["bed_type"] = "2BR"
    df["bath_count"] = 1.0

    # Map status
    df["status"] = (
        df["status_code"].map({"C": "Occupied", "V": "Vacant", "NTV": "Notice"}).fillna("Vacant")
    )

    # Format dates
    for col in ["move_in_date", "lease_start", "lease_end"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")

    # Reorder columns
    columns = [
        "unit_id",
        "floorplan_code",
        "bed_type",
        "bath_count",
        "sqft",
        "market_rent",
        "base_rent",
        "amenity_fee",
        "pet_rent",
        "washer_dryer",
        "concessions",
        "other_income",
        "total_rent",
        "status",
        "resident_name",
        "move_in_date",
        "lease_start",
        "lease_end",
    ]

    return df[[c for c in columns if c in df.columns]]


def generate_summary(df: pd.DataFrame, period: str) -> dict[str, Any]:
    """Generate summary statistics for a rent roll."""
    total_units = len(df)
    occupied = len(df[df["status"] == "Occupied"])
    vacant = len(df[df["status"] == "Vacant"])
    notice = len(df[df["status"] == "Notice"])

    occ_df = df[df["status"] == "Occupied"]

    return {
        "period": period,
        "total_units": total_units,
        "occupied": occupied,
        "vacant": vacant,
        "notice": notice,
        "occupancy_pct": round(occupied / total_units * 100, 1) if total_units > 0 else 0,
        "avg_market_rent": round(df["market_rent"].mean(), 0),
        "avg_contract_rent": round(occ_df["base_rent"].mean(), 0) if len(occ_df) > 0 else 0,
        "avg_total_rent": round(occ_df["total_rent"].mean(), 0) if len(occ_df) > 0 else 0,
        "min_contract_rent": round(occ_df["base_rent"].min(), 0) if len(occ_df) > 0 else 0,
        "max_contract_rent": round(occ_df["base_rent"].max(), 0) if len(occ_df) > 0 else 0,
        "loss_to_lease": round(
            (df["market_rent"].sum() - occ_df["base_rent"].sum()) / df["market_rent"].sum() * 100, 1
        )
        if df["market_rent"].sum() > 0
        else 0,
    }


def main() -> None:
    base_path = Path("reports/sherman-denison-tx/park-place/rent-roll")

    # Process each rent roll
    files = {
        "2025-05": ("RR - Park Place - 5-15-2025.xlsx", "standard"),
        "2025-11": ("Rent-Roll-Park-Place-11.13.2025.xlsx", "resman"),
        "2026-01": ("Rent-Roll-Park-Place-01.2026.xlsx", "resman"),
    }

    all_summaries: list[dict[str, Any]] = []

    for period, (filename, fmt) in files.items():
        file_path = base_path / filename
        print(f"\n{'='*60}")
        print(f"Processing {period}: {filename}")
        print(f"{'='*60}")

        if fmt == "standard":
            df = parse_standard_format(str(file_path))
        else:
            df = parse_resman_multirow(str(file_path))

        # Save standardized CSV
        out_file = base_path / f"rent_roll_standardized_{period.replace('-', '_')}.csv"
        df.to_csv(out_file, index=False)
        print(f"Wrote {len(df)} units to {out_file.name}")

        # Generate summary
        summary = generate_summary(df, period)
        all_summaries.append(summary)

        print(f"\nSummary for {period}:")
        occ = summary["occupied"]
        total = summary["total_units"]
        occ_pct = summary["occupancy_pct"]
        print(f"  Occupancy: {occ}/{total} ({occ_pct}%)")
        print(f"  Vacant: {summary['vacant']}, Notice: {summary['notice']}")
        print(f"  Avg Market Rent: ${summary['avg_market_rent']:,.0f}")
        print(f"  Avg Contract Rent: ${summary['avg_contract_rent']:,.0f}")
        min_rent = summary["min_contract_rent"]
        max_rent = summary["max_contract_rent"]
        print(f"  Contract Rent Range: ${min_rent:,.0f} - ${max_rent:,.0f}")
        print(f"  Loss to Lease: {summary['loss_to_lease']}%")

    # Save summary comparison
    summary_df = pd.DataFrame(all_summaries)
    summary_df.to_csv(base_path / "rent_roll_trend_summary.csv", index=False)
    print("\nWrote trend summary to rent_roll_trend_summary.csv")

    # Print trend analysis
    print("\n" + "=" * 60)
    print("TREND ANALYSIS: May 2025 -> January 2026")
    print("=" * 60)

    may = all_summaries[0]
    jan = all_summaries[2]

    occ_change = jan["occupancy_pct"] - may["occupancy_pct"]
    rent_change = jan["avg_contract_rent"] - may["avg_contract_rent"]
    rent_change_pct = (
        (rent_change / may["avg_contract_rent"] * 100) if may["avg_contract_rent"] > 0 else 0
    )

    may_occ = may["occupancy_pct"]
    jan_occ = jan["occupancy_pct"]
    print(f"Occupancy: {may_occ}% -> {jan_occ}% ({occ_change:+.1f} pts)")
    may_rent = may["avg_contract_rent"]
    jan_rent = jan["avg_contract_rent"]
    print(f"Avg Contract Rent: ${may_rent:,.0f} -> ${jan_rent:,.0f} ({rent_change_pct:+.1f}%)")
    print(f"Vacant Units: {may['vacant']} -> {jan['vacant']}")


if __name__ == "__main__":
    main()
