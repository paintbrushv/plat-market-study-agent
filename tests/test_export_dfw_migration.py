import pandas as pd
from scripts.export_dfw_migration import build_year_flows, DFW_FIPS

CENTROIDS = {
    "06037": (34.31, -118.22),  # LA County
    "40109": (35.55, -97.41),   # Oklahoma County
    "48113": (32.78, -96.80),   # Dallas (intra-MSA, must be excluded)
}


def _inflow_df():
    return pd.DataFrame([
        ["48","113","06","037","CA","Los Angeles County, CA", 1098,1730,129472],
        ["48","113","97","000","TX","Total US, TX",           76092,119745,5830199],  # pseudo
        ["48","113","48","085","TX","Collin County, TX",      5000, 8000, 400000],     # intra-MSA
    ], columns=["y2_statefips","y2_countyfips","y1_statefips","y1_countyfips","y1_state","y1_countyname","n1","n2","agi"])


def _outflow_df():
    return pd.DataFrame([
        ["48","113","40","109","OK","Oklahoma County, OK", 900, 1500, 63900],
        ["48","113","48","439","TX","Tarrant County, TX",  6000, 9000, 480000],       # intra-MSA
    ], columns=["y1_statefips","y1_countyfips","y2_statefips","y2_countyfips","y2_state","y2_countyname","n1","n2","agi"])


def test_build_year_flows_filters_and_aggregates():
    fl = build_year_flows(_inflow_df(), _outflow_df(), CENTROIDS, top_n=150)
    assert [r["fips"] for r in fl["inflow"]] == ["06037"]
    la = fl["inflow"][0]
    assert la["households"] == 1098 and la["people"] == 1730
    assert la["agi_per_return"] == round(129472 * 1000 / 1098)
    assert la["lat"] == 34.31 and la["lon"] == -118.22
    assert la["label"] == "Los Angeles County, CA"
    assert [r["fips"] for r in fl["outflow"]] == ["40109"]
    assert fl["net"]["households"] == 198
    assert fl["net"]["agi"] == (129472 - 63900) * 1000


def test_dfw_fips_has_13():
    assert len(DFW_FIPS) == 13 and "48113" in DFW_FIPS
