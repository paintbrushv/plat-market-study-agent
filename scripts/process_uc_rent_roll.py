"""One-off script to process University Cove rent roll."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def get_bed_type(plan_code: object) -> str:
    """Map floorplan code to bed type."""
    if pd.isna(plan_code):
        return ""
    plan = str(plan_code).upper().strip()
    if "STUDIO" in plan:
        return "Studio"
    elif plan.startswith("A"):
        return "1BR"
    elif plan.startswith("B"):
        return "2BR"
    elif plan.startswith("C"):
        return "3BR"
    return ""


def get_status(status_code: object) -> str:
    """Map status code to standardized status."""
    if pd.isna(status_code):
        return "Vacant"
    s = str(status_code).upper().strip()
    if s == "C":
        return "Occupied"
    elif s == "NTV":
        return "Notice"
    elif s == "V":
        return "Vacant"
    return str(status_code)


def main() -> None:
    # Read the rent roll
    df = pd.read_excel(
        "reports/university-cove-san-antonio/rent-roll/Jan 14th RR UC.v2.xlsx",
        header=7,
    )

    # Filter to unit rows only (exclude totals, blanks, office units)
    df = df[df["Unit"].notna()]
    df = df[~df["Unit"].astype(str).str.contains("Total|Subtotal|Grand", case=False, na=False)]
    df = df[df["Type"] != "OFFIC"]  # Exclude office

    # Create standardized DataFrame
    standardized = pd.DataFrame(
        {
            "unit_id": df["Unit"].astype(str),
            "floorplan_code": df["Type"].astype(str).str.strip(),
            "bed_type": df["Type"].apply(get_bed_type),
            "bath_count": 1.0,
            "sqft": pd.to_numeric(df["Sq. Feet"], errors="coerce"),
            "market_rent": pd.to_numeric(df["Market Rent"], errors="coerce"),
            "lease_rent": pd.to_numeric(df["Rent"], errors="coerce"),
            "status": df["Status"].apply(get_status),
            "resident_name": df["Residents"].fillna(""),
            "move_in_date": pd.to_datetime(df["Move In"], errors="coerce").dt.strftime("%Y-%m-%d"),
            "lease_start": pd.to_datetime(df["Lease Start"], errors="coerce").dt.strftime(
                "%Y-%m-%d"
            ),
            "lease_end": pd.to_datetime(df["Lease End"], errors="coerce").dt.strftime("%Y-%m-%d"),
            "base_rent": pd.to_numeric(df["Rent"], errors="coerce"),
            "pet_rent": None,
            "parking_rent": None,
            "storage_rent": None,
            "utility_reimbursement": None,
            "other_income": pd.to_numeric(df["Other Charges"], errors="coerce"),
            "concessions": pd.to_numeric(df["Credits"], errors="coerce"),
            "total_rent": pd.to_numeric(df["Total"], errors="coerce"),
        }
    )

    # Clean up
    standardized = standardized.fillna("")
    standardized = standardized[standardized["unit_id"] != "nan"]
    standardized = standardized[standardized["market_rent"] != ""]

    # Save standardized CSV
    out_dir = Path("reports/university-cove-san-antonio/rent-roll")
    standardized.to_csv(out_dir / "rent_roll_standardized_v2.csv", index=False)
    print(f"Wrote {len(standardized)} units to rent_roll_standardized_v2.csv")

    # Generate floorplan summary
    summary_data = []
    for plan_code in standardized["floorplan_code"].unique():
        plan_df = standardized[standardized["floorplan_code"] == plan_code]
        bed_type = plan_df["bed_type"].iloc[0] if len(plan_df) > 0 else ""
        units = len(plan_df)
        avg_sqft = plan_df["sqft"].astype(float).mean()
        avg_market_rent = plan_df["market_rent"].astype(float).mean()

        summary_data.append(
            {
                "PlanCode": plan_code,
                "BedType": bed_type,
                "Units": units,
                "SqFt": round(avg_sqft, 0),
                "AvgMarketRent": round(avg_market_rent, 2),
            }
        )

    summary_df = pd.DataFrame(summary_data)
    summary_df = summary_df.sort_values(["BedType", "SqFt", "PlanCode"])
    summary_df.to_csv(out_dir / "floorplan_summary_v2.csv", index=False)
    print(f"Wrote {len(summary_df)} floorplans to floorplan_summary_v2.csv")
    print()
    print("Floorplan Summary:")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
