"""
etl/generate_dfw_watchlist_map.py — DFW Distress & Foreclosure watchlist map.

Reads the geocoded manifest and optional scraped rent/vacancy JSON, then
produces a self-contained MapLibre GL JS HTML map.

Usage:
    uv run python etl/generate_dfw_watchlist_map.py
    uv run python etl/generate_dfw_watchlist_map.py --public
    uv run python etl/generate_dfw_watchlist_map.py --both
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd


MANIFEST_PATH  = Path("reports/dallas-tx/watchlist/dfw_watchlist_manifest.csv")
SCRAPED_PATH   = Path("reports/dallas-tx/watchlist/dfw_watchlist_scraped.json")
DEFAULT_OUT         = Path("reports/dallas-tx/watchlist/dfw_watchlist_map.html")
DEFAULT_OUT_PUBLIC  = Path("reports/dallas-tx/watchlist/dfw_watchlist_map_public.html")

# Map center — DFW
CENTER_LON, CENTER_LAW = -97.03, 32.78

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------
DISTRESS_COLORS: dict[str, str] = {
    "Foreclosure":            "#ef4444",   # red-500
    "Bank-Owned":             "#7f1d1d",   # red-900
    "Auction":                "#f97316",   # orange-500
    "Special Servicing":      "#fb923c",   # orange-400
    "Portfolio Distress":     "#eab308",   # yellow-500
    "Distressed Acquisition": "#84cc16",   # lime-500
    "Pre-Foreclosure":        "#facc15",   # yellow-400
    "Distressed":             "#f59e0b",   # amber-500
}

SPONSOR_BORDERS: dict[str, str] = {
    "Tides":     "#60a5fa",   # blue-400
    "Rise":      "#34d399",   # emerald-400
    "Nitya":     "#c084fc",   # purple-400
    "Lurin":     "#22d3ee",   # cyan-400
    "Windmass":  "#f472b6",   # pink-400
    "Unknown":   "rgba(255,255,255,0.35)",
}

VACANCY_SIGNAL_COLORS: dict[str, str] = {
    "low":      "#22c55e",
    "moderate": "#eab308",
    "high":     "#ef4444",
    "unknown":  "#94a3b8",
    "small_mf": "#475569",   # slate-600 — small/SFR, intentionally dark/muted
}


def distress_severity(category: str) -> int:
    """Return 0–6 severity for sort/sizing."""
    order = ["Pre-Foreclosure", "Distressed Acquisition", "Distressed",
             "Portfolio Distress", "Auction", "Special Servicing",
             "Bank-Owned", "Foreclosure"]
    return order.index(category) if category in order else 3


# ---------------------------------------------------------------------------
# Build GeoJSON
# ---------------------------------------------------------------------------
def build_geojson(manifest: pd.DataFrame, scraped: dict[str, dict]) -> dict:
    features = []

    for _, row in manifest.iterrows():
        lat = row.get("lat")
        lon = row.get("lon")
        if pd.isna(lat) or pd.isna(lon):
            continue

        name        = str(row["property_name"])
        city        = str(row.get("city", ""))
        address     = str(row.get("address", ""))
        status      = str(row.get("status", ""))
        sponsor     = str(row.get("sponsor", "Unknown"))
        if sponsor == "nan":
            sponsor = "Unknown"
        category    = str(row.get("distress_category", "Distressed"))
        full_addr   = f"{address}, {city}, TX"

        sc           = scraped.get(name, {})
        rent_low     = sc.get("rent_low")
        rent_high    = sc.get("rent_high")
        units_avail  = sc.get("units_available")
        vac_signal   = sc.get("vacancy_signal", "unknown")

        # Skip small MF / SFR properties — not meaningful on the map
        if vac_signal == "small_mf":
            continue

        concessions  = sc.get("concessions")
        apt_url      = sc.get("apartments_com_url")
        current_name = sc.get("current_name")  # rebrand name if any

        # Rent label
        if rent_low and rent_high and rent_low != rent_high:
            rent_str = f"${int(rent_low):,}–${int(rent_high):,}"
        elif rent_low:
            rent_str = f"${int(rent_low):,}+"
        else:
            rent_str = None

        # Display name: show both if rebranded
        if current_name and current_name.lower() != name.lower():
            display_name = f"{current_name}"
            rebrand_note = f"(fka {name})"
        else:
            display_name = name
            rebrand_note = None

        props = {
            "name":             name,
            "display_name":     display_name,
            "rebrand_note":     rebrand_note,
            "city":             city,
            "address":          full_addr,
            "status":           status,
            "sponsor":          sponsor,
            "category":         category,
            "distress_color":   DISTRESS_COLORS.get(category, "#f59e0b"),
            "sponsor_border":   SPONSOR_BORDERS.get(sponsor, SPONSOR_BORDERS["Unknown"]),
            "severity":         distress_severity(category),
            # Scraped
            "rent_str":         rent_str,
            "rent_low":         int(rent_low) if rent_low else None,
            "rent_high":        int(rent_high) if rent_high else None,
            "units_available":  int(units_avail) if units_avail is not None else None,
            "vacancy_signal":   vac_signal,
            "vacancy_color":    VACANCY_SIGNAL_COLORS.get(vac_signal, "#94a3b8"),
            "concessions":      concessions,
            "apartments_com_url": apt_url,
            "has_rent_data":    bool(rent_low),
        }

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            "properties": props,
        })

    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Summary stats for legend / header
# ---------------------------------------------------------------------------
def compute_stats(features: list) -> dict:
    cats: dict[str, int] = {}
    sponsors: dict[str, int] = {}
    cities: set = set()
    with_rent = 0
    for f in features:
        p = f["properties"]
        cats[p["category"]] = cats.get(p["category"], 0) + 1
        sp = p["sponsor"]
        if sp != "Unknown":
            sponsors[sp] = sponsors.get(sp, 0) + 1
        cities.add(p["city"])
        if p.get("has_rent_data"):
            with_rent += 1
    return {
        "total": len(features),
        "with_rent": with_rent,
        "categories": dict(sorted(cats.items(), key=lambda x: -x[1])),
        "sponsors": dict(sorted(sponsors.items(), key=lambda x: -x[1])),
        "cities": sorted(cities),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>DFW Multifamily Distress & Foreclosure Map</title>
  <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet"/>
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#f1f5f9;height:100vh;display:flex;flex-direction:column}}

    /* ---- Header ---- */
    #hdr{{
      padding:12px 18px;background:#1e293b;border-bottom:1px solid #334155;
      display:flex;align-items:center;gap:16px;flex-wrap:wrap;flex-shrink:0;
    }}
    #hdr h1{{font-size:14px;font-weight:700;letter-spacing:.02em;color:#f8fafc}}
    #hdr .sub{{font-size:11px;color:#64748b}}
    .controls{{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;align-items:center}}
    .btn{{padding:4px 11px;border-radius:5px;border:1px solid #475569;background:#1e293b;color:#cbd5e1;font-size:11px;cursor:pointer;transition:all .15s}}
    .btn.active{{background:#3b82f6;border-color:#3b82f6;color:#fff}}
    .btn:hover:not(.active){{background:#334155}}
    select.btn{{padding:4px 8px}}

    /* ---- Map ---- */
    #map-wrap{{position:relative;flex:1;min-height:0}}
    #map{{width:100%;height:100%}}

    /* ---- Legend ---- */
    #legend{{
      position:absolute;bottom:36px;left:12px;
      background:rgba(15,23,42,.93);border:1px solid #334155;border-radius:10px;
      padding:13px 15px;min-width:190px;backdrop-filter:blur(4px);
    }}
    #legend h3{{font-size:10px;font-weight:700;color:#64748b;letter-spacing:.08em;text-transform:uppercase;margin-bottom:9px}}
    .lr{{display:flex;align-items:center;gap:7px;font-size:11px;color:#cbd5e1;margin-bottom:5px}}
    .dot{{width:11px;height:11px;border-radius:50%;flex-shrink:0}}
    hr.sep{{border:none;border-top:1px solid #334155;margin:9px 0}}
    .ln{{font-size:10px;color:#475569;margin-top:4px}}

    /* ---- Stats ---- */
    #stats{{
      position:absolute;top:10px;right:10px;
      background:rgba(15,23,42,.93);border:1px solid #334155;border-radius:10px;
      padding:12px 14px;min-width:185px;backdrop-filter:blur(4px);
    }}
    #stats h3{{font-size:10px;font-weight:700;color:#64748b;letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px}}
    .sr{{display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px}}
    .sl{{color:#94a3b8}}.sv{{color:#f1f5f9;font-weight:600}}

    /* ---- Tooltip ---- */
    #tt{{
      position:absolute;pointer-events:none;display:none;z-index:99;
      background:rgba(15,23,42,.97);border:1px solid #475569;border-radius:10px;
      padding:13px 15px;max-width:270px;font-size:12px;
      box-shadow:0 4px 24px rgba(0,0,0,.55);backdrop-filter:blur(4px);
    }}
    .tn{{font-weight:700;font-size:13px;color:#f8fafc;margin-bottom:2px}}
    .ta{{font-size:10px;color:#475569;margin-bottom:8px}}
    .badge{{display:inline-block;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-bottom:8px}}
    .tg{{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:6px}}
    .tl{{font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.05em}}
    .tv{{font-size:13px;font-weight:700;color:#f1f5f9}}
    .tc{{font-size:10px;color:#fbbf24;margin-top:4px}}
    .tsrc{{font-size:9px;color:#334155;margin-top:6px}}
    .tlink{{font-size:10px;color:#60a5fa;margin-top:4px;display:block}}
    hr.td{{border:none;border-top:1px solid #1e293b;margin:7px 0}}
  </style>
</head>
<body>
<div id="hdr">
  <div>
    <h1>DFW Multifamily Distress &amp; Foreclosure Watchlist</h1>
    <div class="sub">{subtitle}</div>
  </div>
  <div class="controls">
    <span style="font-size:11px;color:#475569">Color:</span>
    <button class="btn active" onclick="setMode('distress')" id="btn-distress">Distress Type</button>
    <button class="btn" onclick="setMode('vacancy')" id="btn-vacancy">Vacancy Signal</button>
    <span style="font-size:11px;color:#475569;margin-left:4px">City:</span>
    <select class="btn" id="city-filter" onchange="filterCity(this.value)">
      <option value="">All Cities</option>
      {city_options}
    </select>
  </div>
</div>
<div id="map-wrap">
  <div id="map"></div>

  <div id="legend">
    <div id="legend-distress">
      <h3>Distress Status</h3>
      <div class="lr"><div class="dot" style="background:#ef4444"></div>Foreclosure</div>
      <div class="lr"><div class="dot" style="background:#7f1d1d"></div>Bank-Owned</div>
      <div class="lr"><div class="dot" style="background:#f97316"></div>Auction</div>
      <div class="lr"><div class="dot" style="background:#fb923c"></div>Special Servicing</div>
      <div class="lr"><div class="dot" style="background:#eab308"></div>Portfolio Distress</div>
      <div class="lr"><div class="dot" style="background:#84cc16"></div>Distressed Acquisition</div>
      <div class="lr"><div class="dot" style="background:#facc15"></div>Pre-Foreclosure</div>
      <div class="lr"><div class="dot" style="background:#f59e0b"></div>Distressed (Other)</div>
      <hr class="sep">
      <div class="lr" style="font-size:10px;color:#64748b;font-style:italic">Border = Sponsor portfolio</div>
      <div class="lr"><div class="dot" style="background:#60a5fa;border:2px solid #60a5fa"></div><span style="color:#60a5fa">Tides</span></div>
      <div class="lr"><div class="dot" style="background:#34d399;border:2px solid #34d399"></div><span style="color:#34d399">Rise</span></div>
      <div class="lr"><div class="dot" style="background:#c084fc;border:2px solid #c084fc"></div><span style="color:#c084fc">Nitya</span></div>
      <div class="lr"><div class="dot" style="background:#22d3ee;border:2px solid #22d3ee"></div><span style="color:#22d3ee">Lurin</span></div>
      <div class="lr"><div class="dot" style="background:#f472b6;border:2px solid #f472b6"></div><span style="color:#f472b6">Windmass</span></div>
    </div>
    <div id="legend-vacancy" style="display:none">
      <h3>Vacancy Signal</h3>
      <div class="lr"><div class="dot" style="background:#22c55e"></div>Low (&lt;5% avail)</div>
      <div class="lr"><div class="dot" style="background:#eab308"></div>Moderate (5–15%)</div>
      <div class="lr"><div class="dot" style="background:#ef4444"></div>High (&gt;15%)</div>
      <div class="lr"><div class="dot" style="background:#94a3b8"></div>No data</div>
    </div>
  </div>

  <div id="stats">
    <h3>Watchlist Summary</h3>
    <div id="stats-content"></div>
  </div>
  <div id="tt"></div>
</div>

<script>
const GEOJSON = {geojson_data};
const STATS   = {stats_data};
let mode = 'distress';
let cityFilter = '';

// ---- Map ----
const map = new maplibregl.Map({{
  container: 'map',
  style: {{
    version: 8,
    sources: {{ osm: {{ type:'raster', tiles:['https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png'], tileSize:256, attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>', maxzoom:19 }} }},
    layers: [{{ id:'osm', type:'raster', source:'osm' }}],
  }},
  center: [{center_lon}, {center_lat}],
  zoom: 9.4,
  maxZoom: 17,
}});
map.addControl(new maplibregl.NavigationControl(), 'bottom-right');

// ---- Filter / color helpers ----
function filteredData() {{
  if (!cityFilter) return GEOJSON;
  return {{
    type: 'FeatureCollection',
    features: GEOJSON.features.filter(f => f.properties.city === cityFilter),
  }};
}}

function colorExpr() {{
  if (mode === 'vacancy') return ['coalesce', ['get','vacancy_color'], '#94a3b8'];
  return ['coalesce', ['get','distress_color'], '#f59e0b'];
}}

function setMode(m) {{
  mode = m;
  ['distress','vacancy'].forEach(x => document.getElementById('btn-'+x).classList.toggle('active', x===m));
  document.getElementById('legend-distress').style.display = m==='distress' ? '' : 'none';
  document.getElementById('legend-vacancy').style.display  = m==='vacancy'  ? '' : 'none';
  if (map.getLayer('circles')) map.setPaintProperty('circles','circle-color', colorExpr());
  if (map.getLayer('circles')) map.setPaintProperty('circles','circle-opacity', 0.88);
  updateStats();
}}

function filterCity(val) {{
  cityFilter = val;
  if (map.getSource('props')) map.getSource('props').setData(filteredData());
  updateStats();
}}

// ---- Stats ----
function updateStats() {{
  const feats = filteredData().features;
  const total = feats.length;
  const cats = {{}};
  let withRent = 0;
  feats.forEach(f => {{
    const c = f.properties.category;
    cats[c] = (cats[c]||0)+1;
    if (f.properties.has_rent_data) withRent++;
  }});
  const topCats = Object.entries(cats).sort((a,b)=>b[1]-a[1]).slice(0,4);
  let html = `<div class="sr"><span class="sl">Total properties</span><span class="sv">${{total}}</span></div>`;
  html += `<div class="sr"><span class="sl">With rent data</span><span class="sv">${{withRent}}</span></div>`;
  topCats.forEach(([cat,n]) => {{
    const col = {{
      'Foreclosure':'#ef4444','Bank-Owned':'#7f1d1d','Auction':'#f97316',
      'Special Servicing':'#fb923c','Portfolio Distress':'#eab308',
      'Distressed Acquisition':'#84cc16','Pre-Foreclosure':'#facc15','Distressed':'#f59e0b'
    }}[cat]||'#94a3b8';
    html += `<div class="sr"><span class="sl" style="color:${{col}}">${{cat}}</span><span class="sv">${{n}}</span></div>`;
  }});
  document.getElementById('stats-content').innerHTML = html;
}}

// ---- Map load ----
map.on('load', () => {{
  map.addSource('props', {{ type:'geojson', data: filteredData() }});

  // Shadow
  map.addLayer({{ id:'shadow', type:'circle', source:'props',
    paint:{{ 'circle-radius':10,'circle-color':'rgba(0,0,0,.3)','circle-translate':[2,2] }} }});

  // Main circles
  map.addLayer({{ id:'circles', type:'circle', source:'props',
    paint:{{
      'circle-radius': 9,
      'circle-color': colorExpr(),
      'circle-opacity': 0.88,
      'circle-stroke-width': 2.5,
      'circle-stroke-color': ['get','sponsor_border'],
    }}
  }});

  // Sponsor label (zoom 12+)
  map.addLayer({{ id:'labels', type:'symbol', source:'props',
    minzoom: 12,
    layout:{{
      'text-field': ['get','name'],
      'text-size': 10,
      'text-offset': [0, 1.4],
      'text-anchor': 'top',
      'text-max-width': 10,
      'text-allow-overlap': false,
    }},
    paint:{{ 'text-color':'#e2e8f0','text-halo-color':'rgba(0,0,0,.7)','text-halo-width':1.2 }},
  }});

  // ---- Tooltip ----
  map.on('mouseenter','circles', e => {{
    map.getCanvas().style.cursor = 'pointer';
    const p = e.features[0].properties;
    const tt = document.getElementById('tt');
    const badgeColor = mode==='vacancy' ? p.vacancy_color : p.distress_color;
    const badgeLabel = mode==='vacancy' ? (p.vacancy_signal||'unknown') : p.category;

    const rentRow = p.rent_str
      ? `<div class="tt-cell"><div class="tl">Asking Rent</div><div class="tv">${{p.rent_str}}</div></div>`
      : `<div class="tt-cell"><div class="tl">Asking Rent</div><div class="tv" style="color:#475569">—</div></div>`;

    const availRow = p.units_available !== null && p.units_available !== undefined
      ? `<div class="tt-cell"><div class="tl">Units Avail</div><div class="tv" style="color:${{p.vacancy_color}}">${{p.units_available}}</div></div>`
      : `<div class="tt-cell"><div class="tl">Units Avail</div><div class="tv" style="color:#475569">—</div></div>`;

    const concRow = p.concessions
      ? `<div class="tc">🎁 ${{p.concessions}}</div>` : '';

    const sponsor = p.sponsor !== 'Unknown'
      ? `<span style="color:${{p.sponsor_border}};font-weight:600">${{p.sponsor}}</span> · ` : '';

    const aptLink = p.apartments_com_url
      ? `<a class="tlink" href="${{p.apartments_com_url}}" target="_blank">→ Apartments.com listing</a>` : '';

    const rebrandNote = p.rebrand_note
      ? `<div style="font-size:10px;color:#94a3b8;margin-top:1px">${{p.rebrand_note}}</div>` : '';

    tt.innerHTML = `
      <div class="tn">${{p.display_name || p.name}}</div>
      ${{rebrandNote}}
      <div class="ta">${{p.address}}</div>
      <span class="badge" style="background:${{badgeColor}}22;color:${{badgeColor}};border:1px solid ${{badgeColor}}55">
        ${{p.category}}
      </span><br>
      <div style="font-size:10px;color:#64748b;margin-bottom:8px">${{sponsor}}${{p.status}}</div>
      <div class="tg">${{rentRow}}${{availRow}}</div>
      ${{concRow}}
      ${{aptLink}}
    `;

    positionTT(e.point);
    tt.style.display = 'block';
  }});

  map.on('mousemove','circles', e => positionTT(e.point));
  map.on('mouseleave','circles', () => {{
    map.getCanvas().style.cursor='';
    document.getElementById('tt').style.display='none';
  }});

  map.on('click','circles', e => {{
    map.flyTo({{center: e.features[0].geometry.coordinates, zoom: Math.max(map.getZoom(),13), duration:500}});
  }});

  updateStats();
}});

function positionTT(pt) {{
  const tt = document.getElementById('tt');
  const c  = map.getCanvas();
  let x = pt.x+14, y = pt.y-40;
  if (x+275 > c.clientWidth)  x = pt.x-279;
  if (y < 0) y = pt.y+14;
  tt.style.left = x+'px';
  tt.style.top  = y+'px';
}}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def render(
    geojson: dict,
    stats: dict,
    public: bool = False,
    out: Path | None = None,
) -> Path:
    out = out or (DEFAULT_OUT_PUBLIC if public else DEFAULT_OUT)

    cities = stats["cities"]
    city_opts = "\n      ".join(f'<option value="{c}">{c}</option>' for c in cities)

    n = stats["total"]
    with_rent = stats["with_rent"]
    subtitle_internal = f"{n} properties | {with_rent} with rent data | DFW Metro | March 2026"
    subtitle_public   = f"DFW Metro multifamily distress watchlist · {n} properties · March 2026"
    subtitle = subtitle_public if public else subtitle_internal

    html = HTML.format(
        geojson_data=json.dumps(geojson),
        stats_data=json.dumps(stats),
        subtitle=subtitle,
        city_options=city_opts,
        center_lon=CENTER_LON,
        center_lat=CENTER_LAW,
    )

    # Public: strip any internal references
    if public:
        html = html.replace("Watchlist Summary", "Market Summary")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"Generated: {out}  ({'public' if public else 'internal'})  {len(html):,} chars")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--both",   action="store_true")
    parser.add_argument("--out",    default=None)
    args = parser.parse_args()

    manifest = pd.read_csv(MANIFEST_PATH)

    # Load scraped data if available
    scraped: dict[str, dict] = {}
    if SCRAPED_PATH.exists():
        raw = json.loads(SCRAPED_PATH.read_text())
        if isinstance(raw, list):
            scraped = {r["property_name"]: r for r in raw if "property_name" in r}
        elif isinstance(raw, dict):
            scraped = raw
        print(f"Loaded scraped data for {len(scraped)} properties")
    else:
        print("No scraped data found — map will show distress categories only")

    geojson = build_geojson(manifest, scraped)
    stats   = compute_stats(geojson["features"])

    if args.both:
        render(geojson, stats, public=False)
        render(geojson, stats, public=True)
    else:
        out = Path(args.out) if args.out else None
        render(geojson, stats, public=args.public, out=out)


if __name__ == "__main__":
    main()
