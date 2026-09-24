"""
etl/parse_s2_rent_roll.py — Parser for S2 Residential "Rent Roll Summary" Excel exports.

Handles the merged-cell layout produced by S2 software:
    Row 0   : Property name
    Row 1   : Company (TIC entity)
    Row 2   : "Rent Roll Summary"
    Row 3   : Date (as-of)
    Row 7   : Merged header row — actual data columns at fixed indices
    Row 8+  : One row per unit (unit ID pattern = letter + ≥4 digits)
    Tail    : Floorplan summary + grand-total rows (ignored)

Column map (by zero-based index after reading with header=None):
    0   unit_id
    1   floorplan_code
    3   sqft
    4   resident_name  ("Vacant Unit" for vacants)
    7   status         (C=Current, NTV=Notice, UE=Exempt, MTM=Month-to-Month, NaN=Vacant)
    9   market_rent
    13  lease_rent      (0 for vacants)
    16  other_charges   (utility billing / RUBS)
    18  credits         (one-time rent credits; positive = credit applied)
    20  total_rent
    23  move_in_date
    25  lease_start
    27  lease_end
    28  move_out

Usage:
    uv run python etl/parse_s2_rent_roll.py \\
        --east  "reports/dallas-tx/republics/rent-roll/raw/Republic East - RR - 3.17.26.xlsx" \\
        --west  "reports/dallas-tx/republics/rent-roll/raw/Republic West - RR - 3.17.26.xlsx" \\
        --out   reports/dallas-tx/republics/rent-roll/clean \\
        --expected-units 848 \\
        --summary
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Unit ID pattern: letter(s) followed by ≥4 digits
_UNIT_ID_PAT = re.compile(r"^[A-Z]\d{4,}", re.IGNORECASE)

# Floorplan-to-bed-type mapping (prefix before digit, R suffix = renovated)
def _bed_type(code: str) -> str:
    base = re.sub(r"R$", "", code.upper())
    if base.startswith("A"):
        return "1BR"
    if base.startswith("B"):
        return "2BR"
    if base.startswith("C"):
        return "3BR"
    return "Unknown"


def _status(raw) -> str:
    if pd.isna(raw) or str(raw).strip() == "":
        return "Vacant"
    s = str(raw).strip().upper()
    if s == "NTV":
        return "Notice"
    # C=Current, UE=Unit Exempt (employee), MTM=Month-to-Month — all occupied
    return "Occupied"


def _date(raw) -> str | None:
    if pd.isna(raw):
        return None
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    try:
        return pd.to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_s2_file(path: Path, building_label: str) -> pd.DataFrame:
    """
    Parse a single S2 Rent Roll Summary Excel file.
    Returns a DataFrame in the canonical rent roll schema.
    """
    raw = pd.read_excel(path, sheet_name=0, header=None, dtype={7: str})

    # Find where unit data ends (first row whose col-0 is NOT a unit ID)
    data_start = 8
    data_end = data_start
    for i in range(data_start, len(raw)):
        val = str(raw.iloc[i, 0])
        if not _UNIT_ID_PAT.match(val):
            data_end = i
            break
    else:
        data_end = len(raw)

    df = raw.iloc[data_start:data_end].copy().reset_index(drop=True)

    records = []
    for _, row in df.iterrows():
        unit_id        = str(row.iloc[0]).strip()
        floorplan_code = str(row.iloc[1]).strip()
        sqft           = float(row.iloc[3]) if pd.notna(row.iloc[3]) else None
        resident_name  = str(row.iloc[4]).strip() if pd.notna(row.iloc[4]) else ""
        status_raw     = row.iloc[7]
        market_rent    = float(row.iloc[9])  if pd.notna(row.iloc[9])  else 0.0
        lease_rent     = float(row.iloc[13]) if pd.notna(row.iloc[13]) else 0.0
        other_charges  = float(row.iloc[16]) if pd.notna(row.iloc[16]) else 0.0
        credits        = float(row.iloc[18]) if pd.notna(row.iloc[18]) else 0.0
        total_rent     = float(row.iloc[20]) if pd.notna(row.iloc[20]) else 0.0
        move_in        = _date(row.iloc[23]) if len(row) > 23 else None
        lease_start    = _date(row.iloc[25]) if len(row) > 25 else None
        lease_end      = _date(row.iloc[27]) if len(row) > 27 else None
        move_out       = _date(row.iloc[28]) if len(row) > 28 else None

        status = _status(status_raw)
        bed_type = _bed_type(floorplan_code)
        is_vacant = resident_name == "Vacant Unit"

        # Concessions: credits column stores one-time/monthly credits applied
        # (positive value = money off; store as positive per schema convention)
        concessions = credits if credits > 0 else 0.0

        records.append(
            {
                "building":              building_label,
                "unit_id":               unit_id,
                "floorplan_code":        floorplan_code,
                "bed_type":              bed_type,
                "bath_count":            None,  # not in source
                "sqft":                  sqft,
                "market_rent":           market_rent,
                "lease_rent":            lease_rent,
                "status":                status,
                "resident_name":         "" if is_vacant else resident_name,
                "move_in_date":          move_in,
                "lease_start":           lease_start,
                "lease_end":             lease_end,
                "move_out_date":         move_out,
                "base_rent":             lease_rent,       # S2 rolls up; no code split
                "pet_rent":              0.0,
                "parking_rent":          0.0,
                "storage_rent":          0.0,
                "utility_reimbursement": other_charges,    # RUBS / utility billing
                "other_income":          0.0,
                "concessions":           concessions,
                "total_rent":            total_rent,
            }
        )

    result = pd.DataFrame(records)
    return result


# ---------------------------------------------------------------------------
# Floorplan summary
# ---------------------------------------------------------------------------

def build_floorplan_summary(df: pd.DataFrame) -> pd.DataFrame:
    occupied = df[df["status"].isin(["Occupied", "Notice"])]
    grp = (
        df.groupby(["floorplan_code", "bed_type"])
        .agg(
            Units=("unit_id", "count"),
            SqFt=("sqft", "mean"),
            AvgMarketRent=("market_rent", "mean"),
            OccUnits=("unit_id", lambda x: (df.loc[x.index, "status"].isin(["Occupied", "Notice"])).sum()),
        )
        .reset_index()
        .rename(columns={"floorplan_code": "PlanCode", "bed_type": "BedType"})
    )
    grp["Occupancy%"] = grp["OccUnits"] / grp["Units"]
    grp["SqFt"] = grp["SqFt"].round(0).astype(int)
    grp["AvgMarketRent"] = grp["AvgMarketRent"].round(2)
    grp["Occupancy%"] = grp["Occupancy%"].round(4)
    sort_order = {"1BR": 0, "2BR": 1, "3BR": 2, "Unknown": 9}
    grp["_sort"] = grp["BedType"].map(sort_order)
    grp = grp.sort_values(["_sort", "PlanCode"]).drop(columns=["_sort"])
    return grp


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(df: pd.DataFrame, expected_units: int | None) -> list[str]:
    errors: list[str] = []
    actual = len(df)
    if expected_units and abs(actual - expected_units) > 2:
        errors.append(f"Unit count mismatch: got {actual}, expected {expected_units} (±2)")

    null_critical = df[["unit_id", "floorplan_code", "bed_type", "sqft", "market_rent"]].isnull().sum()
    for col, n in null_critical.items():
        if n > 0:
            errors.append(f"Null in critical column '{col}': {n} rows")

    zero_mkt_occupied = df[(df["status"].isin(["Occupied", "Notice"])) & (df["market_rent"] == 0)]
    if len(zero_mkt_occupied) > 0:
        errors.append(f"$0 market rent on {len(zero_mkt_occupied)} occupied units")

    occ_rate = df["status"].isin(["Occupied", "Notice"]).mean()
    if not (0.50 <= occ_rate <= 1.00):
        errors.append(f"Occupancy {occ_rate:.1%} outside 50–100% range")

    unknown_bt = df[df["bed_type"] == "Unknown"]
    if len(unknown_bt) > 0:
        codes = unknown_bt["floorplan_code"].unique().tolist()
        errors.append(f"Unknown bed_type for {len(unknown_bt)} units (plans: {codes})")

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Parse two S2 Rent Roll Summary files and combine")
    parser.add_argument("--east",  required=True, help="Path to East building Excel file")
    parser.add_argument("--west",  required=True, help="Path to West building Excel file")
    parser.add_argument("--out",   required=True, help="Output directory")
    parser.add_argument("--expected-units", type=int, default=None)
    parser.add_argument("--summary", action="store_true", help="Print floorplan summary")
    parser.add_argument("--snapshot-date", default=None, help="YYYY-MM override (default: from filename)")
    args = parser.parse_args()

    # Parse
    east_df = parse_s2_file(Path(args.east), "East")
    west_df = parse_s2_file(Path(args.west), "West")
    combined = pd.concat([east_df, west_df], ignore_index=True)

    # Validate
    errors = validate(combined, args.expected_units)
    if errors:
        print("\n⚠  VALIDATION ISSUES:", file=sys.stderr)
        for e in errors:
            print(f"   • {e}", file=sys.stderr)
        if args.expected_units and any("Unit count mismatch" in e for e in errors):
            sys.exit(1)

    # Snapshot date
    if args.snapshot_date:
        snap = args.snapshot_date
    else:
        # Try to extract from filename: "3.17.26" → 2026-03
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", Path(args.east).name)
        if m:
            mo, _d, yr = m.group(1), m.group(2), m.group(3)
            snap = f"20{yr.zfill(2)}-{mo.zfill(2)}"
        else:
            snap = datetime.today().strftime("%Y-%m")

    out_dir = Path(args.out)
    snap_dir = out_dir / "snapshots" / snap
    hist_dir = out_dir / "history"
    for d in [out_dir, snap_dir, hist_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Write standardized CSV
    combined.to_csv(out_dir / "rent_roll_standardized.csv", index=False)
    combined.to_csv(snap_dir / "rent_roll_standardized.csv", index=False)
    print(f"Wrote {len(combined)} units → {out_dir}/rent_roll_standardized.csv")

    # Floorplan summary
    fp = build_floorplan_summary(combined)
    fp.to_csv(out_dir / "floorplan_summary.csv", index=False)
    fp.to_csv(snap_dir / "floorplan_summary.csv", index=False)
    print(f"Wrote floorplan summary ({len(fp)} plans) → {out_dir}/floorplan_summary.csv")

    # Parquet history
    hist_path = hist_dir / "rent_roll_history.parquet"
    combined["snapshot_date"] = snap
    if hist_path.exists():
        existing = pd.read_parquet(hist_path)
        existing = existing[existing["snapshot_date"] != snap]
        updated = pd.concat([existing, combined], ignore_index=True)
    else:
        updated = combined
    updated.to_parquet(hist_path, index=False)

    fp_hist_path = hist_dir / "floorplan_history.parquet"
    fp["snapshot_date"] = snap
    if fp_hist_path.exists():
        fp_ex = pd.read_parquet(fp_hist_path)
        fp_ex = fp_ex[fp_ex["snapshot_date"] != snap]
        fp_updated = pd.concat([fp_ex, fp], ignore_index=True)
    else:
        fp_updated = fp
    fp_updated.to_parquet(fp_hist_path, index=False)

    if args.summary or True:
        total_units = len(combined)
        occ = combined["status"].isin(["Occupied", "Notice"])
        ntv = combined["status"] == "Notice"
        vac = combined["status"] == "Vacant"
        occ_rate = occ.mean()
        avg_mkt_occ = combined[occ]["market_rent"].mean()
        avg_lease_occ = combined[occ]["lease_rent"].mean()

        print()
        print("=" * 62)
        print(f"  The Republics W+E  |  Rent Roll  |  {snap}")
        print("=" * 62)
        print(f"  Total units:        {total_units}")
        print(f"    East:             {len(east_df)}")
        print(f"    West:             {len(west_df)}")
        print(f"  Occupied:           {occ.sum()}  ({occ_rate:.1%})")
        print(f"  Notice to Vacate:   {ntv.sum()}")
        print(f"  Vacant:             {vac.sum()}  ({vac.mean():.1%})")
        print(f"  Avg market rent (occ): ${avg_mkt_occ:,.0f}")
        print(f"  Avg lease rent (occ):  ${avg_lease_occ:,.0f}")
        print(f"  Loss-to-lease:         ${avg_mkt_occ - avg_lease_occ:+,.0f}")
        print()
        print("  Floorplan Summary:")
        print(f"  {'Plan':<8} {'Bed':<5} {'Units':>5} {'Occ':>4} {'SqFt':>5} "
              f"{'AvgMkt':>8} {'Occ%':>6}")
        print(f"  {'-'*7} {'-'*4} {'-'*5} {'-'*4} {'-'*5} {'-'*8} {'-'*6}")
        for _, r in fp.iterrows():
            print(f"  {r.PlanCode:<8} {r.BedType:<5} {r.Units:>5} "
                  f"{r.OccUnits:>4} {r.SqFt:>5} "
                  f"${r.AvgMarketRent:>7,.0f} {r['Occupancy%']:>6.1%}")
        print("=" * 62)

        # By bed type
        print()
        print("  By Bed Type:")
        print(f"  {'BedType':<8} {'Units':>6} {'Occ':>5} {'Vac':>5} {'Occ%':>6} "
              f"{'AvgMkt':>8} {'AvgLse':>8}")
        print(f"  {'-'*7} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*8} {'-'*8}")
        for bt, grp in combined.groupby("bed_type"):
            bt_occ = grp["status"].isin(["Occupied", "Notice"])
            print(f"  {bt:<8} {len(grp):>6} {bt_occ.sum():>5} {(~bt_occ).sum():>5} "
                  f"{bt_occ.mean():>6.1%} "
                  f"${grp['market_rent'].mean():>7,.0f} "
                  f"${grp[bt_occ]['lease_rent'].mean():>7,.0f}")
        print("=" * 62)

        if errors:
            print(f"\n  ⚠  {len(errors)} validation issue(s) — see stderr")


if __name__ == "__main__":
    main()
