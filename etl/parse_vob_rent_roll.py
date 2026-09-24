"""
etl/parse_vob_rent_roll.py — Parser for Verandas of Beaumont portfolio export.

File layout:
    Row 0  : "Portfolios: Verandas Of Beaumont"  (skip)
    Row 1  : Column headers: Unit, BD/BA, Tenant, Sqft, Market Rent, Rent,
             Lease From, Lease To, Move-in, Move-out
    Row 2  : Property name subheader (skip)
    Row 3+ : One row per unit (100 units)
    No footer rows.

BD/BA format: "2/1.00" → bed_type=2BR, bath_count=1
Status logic:
    - No Tenant / NaN  → Vacant
    - "SHOW UNIT"      → Vacant (model unit)
    - Has Tenant       → Occupied
    - Has Tenant + Move-out date populated → Notice

Snapshot date extracted from filename: "3-16-26" → 2026-03

Usage:
    uv run python etl/parse_vob_rent_roll.py \\
        "reports/beaumont-tx/verandas-of-beaumont/rent-roll/raw/VOB-Rent-Roll-3-16-26.xlsx" \\
        --expected-units 100 --summary
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_bdba(raw: str) -> tuple[str, int | None]:
    """Parse 'N/M.00' → (bed_type, bath_count). E.g. '2/1.00' → ('2BR', 1)."""
    m = re.match(r"^(\d+)/(\d+)", str(raw).strip())
    if not m:
        return "Unknown", None
    beds = int(m.group(1))
    baths = int(float(m.group(2)))
    if beds == 0:
        bed_type = "Studio"
    else:
        bed_type = f"{beds}BR"
    return bed_type, baths


def _date(raw) -> str | None:
    if pd.isna(raw):
        return None
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    try:
        return pd.to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return None


def _status(tenant_raw, move_out_raw) -> str:
    tenant = str(tenant_raw).strip() if pd.notna(tenant_raw) else ""
    if not tenant or tenant.upper() in ("SHOW UNIT", "VACANT"):
        return "Vacant"
    if pd.notna(move_out_raw):
        # Move-out date populated = Notice to Vacate
        return "Notice"
    return "Occupied"


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_vob_file(path: Path) -> pd.DataFrame:
    """Parse the VOB portfolio export. Returns canonical rent roll DataFrame."""
    # Row 0 = portfolio header, Row 1 = col headers, Row 2 = property subheader
    # skiprows=[0, 2] keeps row 1 as header and skips the property name row
    raw = pd.read_excel(path, header=1, skiprows=[2], dtype=str)

    records = []
    for _, row in raw.iterrows():
        unit_id = str(row.get("Unit", "")).strip()
        if not unit_id or not unit_id.isdigit():
            continue  # skip any stray non-unit rows

        bdba_raw = row.get("BD/BA", "")
        bed_type, bath_count = _parse_bdba(bdba_raw)

        tenant = row.get("Tenant", "")
        tenant_str = str(tenant).strip() if pd.notna(tenant) else ""

        sqft_raw = row.get("Sqft")
        sqft = float(sqft_raw) if pd.notna(sqft_raw) and str(sqft_raw).strip() != "" else None

        mkt_raw = row.get("Market Rent")
        market_rent = float(mkt_raw) if pd.notna(mkt_raw) and str(mkt_raw).strip() != "" else 0.0

        rent_raw = row.get("Rent")
        lease_rent = float(rent_raw) if pd.notna(rent_raw) and str(rent_raw).strip() != "" else 0.0

        lease_from = _date(row.get("Lease From"))
        lease_to   = _date(row.get("Lease To"))
        move_in    = _date(row.get("Move-in"))
        move_out   = _date(row.get("Move-out"))

        status = _status(tenant, row.get("Move-out"))
        is_vacant = status == "Vacant"
        resident = "" if is_vacant or tenant_str.upper() == "SHOW UNIT" else tenant_str

        # Floorplan code derived from BD/BA (e.g. "2/1.00" → "2BR-1BA")
        floorplan_code = f"{bed_type}-1BA"

        records.append({
            "unit_id":               unit_id,
            "floorplan_code":        floorplan_code,
            "bed_type":              bed_type,
            "bath_count":            bath_count,
            "sqft":                  sqft,
            "market_rent":           market_rent,
            "lease_rent":            lease_rent,
            "status":                status,
            "resident_name":         resident,
            "move_in_date":          move_in,
            "lease_start":           lease_from,
            "lease_end":             lease_to,
            "move_out_date":         move_out,
            "base_rent":             lease_rent,
            "pet_rent":              0.0,
            "parking_rent":          0.0,
            "storage_rent":          0.0,
            "utility_reimbursement": 0.0,
            "other_income":          0.0,
            "concessions":           0.0,
            "total_rent":            lease_rent,
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Floorplan summary
# ---------------------------------------------------------------------------

def build_floorplan_summary(df: pd.DataFrame) -> pd.DataFrame:
    occ_mask = df["status"].isin(["Occupied", "Notice"])
    grp = (
        df.groupby(["floorplan_code", "bed_type"])
        .agg(
            Units=("unit_id", "count"),
            SqFt=("sqft", "mean"),
            AvgMarketRent=("market_rent", "mean"),
            OccUnits=("unit_id", lambda x: occ_mask.loc[x.index].sum()),
        )
        .reset_index()
        .rename(columns={"floorplan_code": "PlanCode", "bed_type": "BedType"})
    )
    grp["Occupancy%"] = grp["OccUnits"] / grp["Units"]
    grp["SqFt"] = grp["SqFt"].round(0).astype(int)
    grp["AvgMarketRent"] = grp["AvgMarketRent"].round(2)
    grp["Occupancy%"] = grp["Occupancy%"].round(4)
    sort_order = {"Studio": 0, "1BR": 1, "2BR": 2, "3BR": 3, "4BR": 4, "Unknown": 9}
    grp["_sort"] = grp["BedType"].map(sort_order)
    return grp.sort_values(["_sort", "PlanCode"]).drop(columns=["_sort"])


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

    zero_mkt_occ = df[(df["status"].isin(["Occupied", "Notice"])) & (df["market_rent"] == 0)]
    if len(zero_mkt_occ) > 0:
        errors.append(f"$0 market rent on {len(zero_mkt_occ)} occupied units")

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
    parser = argparse.ArgumentParser(description="Parse Verandas of Beaumont rent roll export")
    parser.add_argument("input", help="Path to VOB Excel rent roll")
    parser.add_argument("--out", default=None, help="Output directory (default: rent-roll/clean/ sibling of raw/)")
    parser.add_argument("--expected-units", type=int, default=100)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--snapshot-date", default=None, help="YYYY-MM override")
    args = parser.parse_args()

    input_path = Path(args.input)

    # Parse
    df = parse_vob_file(input_path)

    # Validate
    errors = validate(df, args.expected_units)
    if errors:
        print("\n⚠  VALIDATION ISSUES:", file=sys.stderr)
        for e in errors:
            print(f"   • {e}", file=sys.stderr)
        if any("Unit count mismatch" in e for e in errors):
            sys.exit(1)

    # Snapshot date from filename: "3-16-26" → 2026-03
    if args.snapshot_date:
        snap = args.snapshot_date
    else:
        m = re.search(r"(\d+)-(\d+)-(\d+)", input_path.name)
        if m:
            mo, _d, yr = m.group(1), m.group(2), m.group(3)
            snap = f"20{yr.zfill(2)}-{mo.zfill(2)}"
        else:
            snap = datetime.today().strftime("%Y-%m")

    # Output directory: default to rent-roll/clean/ alongside raw/
    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = input_path.parent.parent / "clean"

    snap_dir = out_dir / "snapshots" / snap
    hist_dir = out_dir / "history"
    for d in [out_dir, snap_dir, hist_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Write standardized CSV
    df.to_csv(out_dir / "rent_roll_standardized.csv", index=False)
    df.to_csv(snap_dir / "rent_roll_standardized.csv", index=False)
    print(f"Wrote {len(df)} units → {out_dir}/rent_roll_standardized.csv")

    # Floorplan summary
    fp = build_floorplan_summary(df)
    fp.to_csv(out_dir / "floorplan_summary.csv", index=False)
    fp.to_csv(snap_dir / "floorplan_summary.csv", index=False)
    print(f"Wrote floorplan summary ({len(fp)} plans) → {out_dir}/floorplan_summary.csv")

    # Parquet history
    hist_path = hist_dir / "rent_roll_history.parquet"
    df["snapshot_date"] = snap
    if hist_path.exists():
        existing = pd.read_parquet(hist_path)
        existing = existing[existing["snapshot_date"] != snap]
        updated = pd.concat([existing, df], ignore_index=True)
    else:
        updated = df
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

    if args.summary:
        occ = df["status"].isin(["Occupied", "Notice"])
        ntv = df["status"] == "Notice"
        vac = df["status"] == "Vacant"
        occ_df = df[occ]
        avg_mkt = occ_df["market_rent"].mean() if len(occ_df) else 0
        avg_lease = occ_df["lease_rent"].mean() if len(occ_df) else 0

        print()
        print("=" * 60)
        print(f"  Verandas of Beaumont  |  Rent Roll  |  {snap}")
        print("=" * 60)
        print(f"  Total units:        {len(df)}")
        print(f"  Occupied:           {occ.sum()}  ({occ.mean():.1%})")
        print(f"  Notice to Vacate:   {ntv.sum()}")
        print(f"  Vacant:             {vac.sum()}  ({vac.mean():.1%})")
        print(f"  Avg market rent (occ): ${avg_mkt:,.0f}")
        print(f"  Avg lease rent (occ):  ${avg_lease:,.0f}")
        print(f"  GTL/LTL:               ${avg_lease - avg_mkt:+,.0f}/unit")
        print()
        print(f"  {'Plan':<10} {'Bed':<5} {'Units':>5} {'Occ':>4} {'SqFt':>5} {'AvgMkt':>8} {'Occ%':>6}")
        print(f"  {'-'*9} {'-'*4} {'-'*5} {'-'*4} {'-'*5} {'-'*8} {'-'*6}")
        for _, r in fp.iterrows():
            print(f"  {r.PlanCode:<10} {r.BedType:<5} {r.Units:>5} "
                  f"{r.OccUnits:>4} {r.SqFt:>5} "
                  f"${r.AvgMarketRent:>7,.0f} {r['Occupancy%']:>6.1%}")
        print("=" * 60)

        if errors:
            print(f"\n  ⚠  {len(errors)} validation issue(s) — see stderr")


if __name__ == "__main__":
    main()
