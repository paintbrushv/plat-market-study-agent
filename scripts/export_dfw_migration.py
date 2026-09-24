#!/usr/bin/env python3
"""Export DFW migration & income flows as dfw-migration.json.

Reads raw IRS SOI county inflow/outflow CSVs, aggregates flows for the 13-county
Dallas–Fort Worth–Arlington MSA (excluding intra-MSA and pseudo/foreign rows),
joins county centroids, writes one static JSON. Public-domain data only.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pandas as pd

SOI_DIR = Path(__file__).resolve().parent.parent / "data" / "public" / "irs_soi_migration"
CENTROID_CSV = Path(__file__).resolve().parent.parent / "data" / "public" / "gazetteer" / "county_centroids.csv"
OUT = Path("dfw-migration.json")

DFW_FIPS = {
    "48085", "48113", "48121", "48139", "48221", "48231", "48251",
    "48257", "48367", "48397", "48425", "48439", "48497",
}
DFW_ANCHOR = {"lat": 32.78, "lon": -96.9}

YEAR_FILES = [("1819", "2018-19"), ("1920", "2019-20"), ("2021", "2020-21"),
              ("2122", "2021-22"), ("2223", "2022-23")]
TOP_N = 150


def load_centroids() -> dict:
    out = {}
    with CENTROID_CSV.open() as f:
        for r in csv.DictReader(f):
            try:
                out[r["fips"]] = (round(float(r["lat"]), 2), round(float(r["lon"]), 2))
            except (ValueError, KeyError):
                continue
    return out


def _fips(state: str, county: str) -> str:
    return f"{int(state):02d}{int(county):03d}"


def build_year_flows(inflow_df, outflow_df, centroids, top_n=TOP_N) -> dict:
    def agg_with_totals(df, dfw_cols, other_cols):
        dfw_s, dfw_c = dfw_cols
        oth_s, oth_c, oth_name = other_cols
        by = {}
        for _, r in df.iterrows():
            try:
                dfw_fips = _fips(r[dfw_s], r[dfw_c])
                other_fips = _fips(r[oth_s], r[oth_c])
            except (ValueError, TypeError):
                continue
            if dfw_fips not in DFW_FIPS:
                continue
            if other_fips in DFW_FIPS:
                continue
            if other_fips not in centroids:
                continue
            try:
                n1 = int(r["n1"])
            except (ValueError, TypeError):
                continue
            if n1 <= 0:
                continue
            b = by.setdefault(other_fips, {"fips": other_fips, "label": str(r[oth_name]).strip(),
                                           "n1": 0, "n2": 0, "agi_k": 0.0})
            b["n1"] += n1
            b["n2"] += int(r["n2"])
            b["agi_k"] += float(r["agi"])
        records = []
        total_agi = 0.0
        for b in by.values():
            lat, lon = centroids[b["fips"]]
            total_agi += b["agi_k"] * 1000
            records.append({
                "fips": b["fips"], "label": b["label"], "lat": lat, "lon": lon,
                "households": b["n1"], "people": b["n2"],
                "agi_per_return": round(b["agi_k"] * 1000 / b["n1"]),
            })
        return records, total_agi

    inflow, inflow_agi = agg_with_totals(inflow_df, ("y2_statefips", "y2_countyfips"),
                                         ("y1_statefips", "y1_countyfips", "y1_countyname"))
    outflow, outflow_agi = agg_with_totals(outflow_df, ("y1_statefips", "y1_countyfips"),
                                           ("y2_statefips", "y2_countyfips", "y2_countyname"))

    net_house = sum(r["households"] for r in inflow) - sum(r["households"] for r in outflow)
    net_agi = round(inflow_agi - outflow_agi)

    inflow.sort(key=lambda r: r["households"], reverse=True)
    outflow.sort(key=lambda r: r["households"], reverse=True)
    return {"inflow": inflow[:top_n], "outflow": outflow[:top_n],
            "net": {"households": net_house, "agi": net_agi}}


def run():
    centroids = load_centroids()
    flows, years = {}, []
    for code, label in YEAR_FILES:
        inf = SOI_DIR / f"countyinflow{code}.csv"
        out = SOI_DIR / f"countyoutflow{code}.csv"
        if not inf.exists() or not out.exists():
            print(f"  skip {label}: missing files")
            continue
        idf = pd.read_csv(inf, dtype=str, encoding="latin-1")
        odf = pd.read_csv(out, dtype=str, encoding="latin-1")
        idf["y1_countyname"] = idf["y1_countyname"].str.strip() + ", " + idf["y1_state"].str.strip()
        odf["y2_countyname"] = odf["y2_countyname"].str.strip() + ", " + odf["y2_state"].str.strip()
        flows[label] = build_year_flows(idf, odf, centroids)
        years.append(label)
        print(f"  {label}: inflow {len(flows[label]['inflow'])}, outflow {len(flows[label]['outflow'])}")

    doc = {
        "meta": {
            "as_of": years[-1] if years else None,
            "msa": "Dallas–Fort Worth–Arlington, TX",
            "source": "IRS SOI county-to-county migration (public domain)",
            "dfw": DFW_ANCHOR,
            "note": ("US inter-county flows; excludes intra-MSA moves and foreign. "
                     "Returns = households, AGI in USD."),
            "truncated_to": TOP_N,
        },
        "years": years,
        "flows": flows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
