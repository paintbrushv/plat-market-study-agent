#!/usr/bin/env python3
"""Download + cache US county centroids (FIPS, lat, lon) from the Census
Gazetteer. Public domain. Stdlib only (no requests). Run once; cached after."""
from __future__ import annotations

import csv
import io
import socket
import sys
import urllib.request
import zipfile
from pathlib import Path

socket.setdefaulttimeout(60)  # never hang the pipeline

URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
       "2023_Gazetteer/2023_Gaz_counties_national.zip")
OUT = Path(__file__).resolve().parent.parent / "data" / "public" / "gazetteer" / "county_centroids.csv"


def run():
    if OUT.exists() and OUT.stat().st_size > 0:
        print(f"cache exists: {OUT}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {URL} ...", flush=True)
    req = urllib.request.Request(URL, headers={"User-Agent": "market-study-agent/1.0"})
    with urllib.request.urlopen(req) as resp:
        zraw = resp.read()
    zf = zipfile.ZipFile(io.BytesIO(zraw))
    member = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
    text = zf.read(member).decode("latin-1")
    rows = []
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    for r in reader:
        # Gazetteer column names carry trailing whitespace; normalize keys.
        rec = {k.strip(): (v.strip() if v else v) for k, v in r.items()}
        geoid = rec.get("GEOID")
        lat, lon = rec.get("INTPTLAT"), rec.get("INTPTLONG")
        if geoid and lat and lon:
            rows.append((geoid, lat, lon))
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fips", "lat", "lon"])
        w.writerows(rows)
    print(f"wrote {len(rows)} county centroids -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
