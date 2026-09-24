#!/usr/bin/env python3
"""
Generate "$/SF vs unit size" scatter plots from comp analysis Markdown.

This parses the comp analysis .md files produced by this repo (which include summary
tables with SF and effective $/SF) and produces presentation-ready PNG + SVG plots,
plus optional interactive HTML versions.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.artist import Artist
from matplotlib.axes import Axes


@dataclass(frozen=True)
class RentPoint:
    unit_type: str
    property_name: str
    sf: float
    eff_psf: float
    is_subject: bool


class _AxisPayload(TypedDict):
    x: str
    y: str


class _PointPayload(TypedDict):
    property: str
    sf: float
    eff_psf: float
    is_subject: bool


class _TrendPayload(TypedDict):
    m: float
    b: float


class _SeriesPayload(TypedDict):
    unit_type: str
    color: str
    marker: str
    points: list[_PointPayload]
    trend: _TrendPayload | None
    subject_name: str | None


class InteractivePayload(TypedDict):
    title: str
    subtitle: str | None
    xlim: list[float]
    ylim: list[float]
    series: list[_SeriesPayload]
    axis: _AxisPayload
    note: str


_HEADING_RE = re.compile(r"^(?P<level>#{2,6})\s+(?P<title>.+?)\s*$")


def _strip_md(text: str) -> str:
    cleaned = re.sub(r"[*`_]", "", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _parse_money(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned or cleaned == "-" or cleaned.lower() == "n/a":
        return None
    cleaned = re.sub(r"[$,%]", "", cleaned)
    cleaned = cleaned.replace(",", "")
    cleaned = cleaned.strip()
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_number(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned or cleaned == "-" or cleaned.lower() == "n/a":
        return None
    cleaned = cleaned.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _normalize_unit_type(section_title: str) -> str | None:
    title = section_title.lower()
    if "studio" in title:
        return "Studio"
    if "1-bedroom" in title or "1 bedroom" in title or "1br" in title or "1-bed" in title:
        return "1BR"
    if "2-bedroom" in title or "2 bedroom" in title or "2br" in title or "2-bed" in title:
        return "2BR"
    if "3-bedroom" in title or "3 bedroom" in title or "3br" in title or "3-bed" in title:
        return "3BR"
    return None


def _iter_markdown_tables(lines: list[str]) -> Iterable[tuple[int, list[str]]]:
    """
    Yield (start_index, table_lines) for GitHub-flavored markdown tables.
    """
    i = 0
    while i + 1 < len(lines):
        line = lines[i].rstrip("\n")
        next_line = lines[i + 1].rstrip("\n")
        if (
            line.lstrip().startswith("|")
            and next_line.lstrip().startswith("|")
            and "---" in next_line
        ):
            table = [line]
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                table.append(lines[j].rstrip("\n"))
                j += 1
            yield i, table
            i = j
            continue
        i += 1


def _split_table_row(row: str) -> list[str]:
    # Trim leading/trailing | then split.
    trimmed = row.strip().strip("|")
    return [cell.strip() for cell in trimmed.split("|")]


def _extract_metadata(md_text: str) -> tuple[str | None, str | None]:
    """
    Return (property_name, as_of_date_text) when present.
    """
    property_name: str | None = None
    as_of: str | None = None
    for line in md_text.splitlines():
        if property_name is None:
            m = re.match(r"^\*\*Property:\*\*\s*(.+?)\s*$", line.strip())
            if m:
                property_name = _strip_md(m.group(1))
                continue
        if as_of is None:
            m = re.match(
                r"^\*\*(As[- ]?Of Date|As Of Date|As-Of Date):\*\*\s*(.+?)\s*$", line.strip()
            )
            if m:
                as_of = _strip_md(m.group(2))
                continue
        if property_name is not None and as_of is not None:
            break
    return property_name, as_of


def extract_rent_points(md_path: Path) -> list[RentPoint]:
    md_text = md_path.read_text(encoding="utf-8")
    lines = md_text.splitlines(keepends=False)
    subject_name, _ = _extract_metadata(md_text)

    current_section = ""
    section_by_line: dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            current_section = m.group("title").strip()
        section_by_line[i] = current_section

    points: list[RentPoint] = []
    for start_idx, table_lines in _iter_markdown_tables(lines):
        section_title = section_by_line.get(start_idx, "") or ""
        unit_type = _normalize_unit_type(section_title)
        if unit_type is None:
            continue

        header = _split_table_row(table_lines[0])
        header_l = [h.lower() for h in header]

        def find_col(
            predicate: Callable[[str], bool],
            header_cols: list[str] = header_l,
        ) -> int | None:
            for idx, col in enumerate(header_cols):
                if predicate(col):
                    return idx
            return None

        property_col = find_col(lambda c: "property" in c or "community" in c)
        sf_col = find_col(
            lambda c: c.strip() in {"sf"} or "avg sf" in c or "sq ft" in c or "sqft" in c
        )
        psf_col = find_col(lambda c: "/sf" in c)
        effective_col = find_col(lambda c: "effective" in c)

        if property_col is None or sf_col is None:
            continue
        if psf_col is None and effective_col is None:
            continue

        # Skip the separator row (table_lines[1]).
        for raw_row in table_lines[2:]:
            row = _split_table_row(raw_row)
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            prop_raw = row[property_col]
            prop = _strip_md(prop_raw)
            if not prop:
                continue
            prop_l = prop.lower()
            if ("comp avg" in prop_l) or ("market avg" in prop_l) or prop_l.endswith("avg"):
                continue

            sf = _parse_number(row[sf_col]) if sf_col < len(row) else None
            if sf is None or sf <= 0:
                continue

            eff_psf: float | None = None
            if psf_col is not None and psf_col < len(row):
                eff_psf = _parse_money(row[psf_col])
            if eff_psf is None and effective_col is not None and effective_col < len(row):
                eff_rent = _parse_money(row[effective_col])
                if eff_rent is not None:
                    eff_psf = eff_rent / sf
            if eff_psf is None or eff_psf <= 0:
                continue

            is_subject = ("subject" in prop_l) or (
                subject_name is not None and prop == subject_name
            )
            points.append(
                RentPoint(
                    unit_type=unit_type,
                    property_name=prop,
                    sf=float(sf),
                    eff_psf=float(eff_psf),
                    is_subject=is_subject,
                )
            )

    # Deduplicate exact duplicates (some reports repeat tables in different sections).
    uniq: dict[tuple[str, str, float, float, bool], RentPoint] = {}
    for p in points:
        key = (p.unit_type, p.property_name, round(p.sf, 2), round(p.eff_psf, 4), p.is_subject)
        uniq[key] = p
    return list(uniq.values())


def _palette_for(unit_type: str) -> str:
    return {
        "Studio": "#1f77b4",  # blue
        "1BR": "#2ca02c",  # green
        "2BR": "#9467bd",  # purple
        "3BR": "#ff7f0e",  # orange
    }.get(unit_type, "#7f7f7f")


def _marker_for(unit_type: str) -> str:
    return {
        "Studio": "o",
        "1BR": "^",
        "2BR": "s",
        "3BR": "D",
    }.get(unit_type, "o")


def _apply_axes_style(ax: Axes) -> None:
    ax.grid(True, alpha=0.25, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ["top", "right"]:
        ax.spines[side].set_visible(False)


def plot_rent_sf(points: list[RentPoint], title: str, subtitle: str | None, out_base: Path) -> None:
    if not points:
        raise ValueError("No rent points found to plot.")

    # Stable unit type ordering.
    unit_order = ["Studio", "1BR", "2BR", "3BR"]
    unit_types = [u for u in unit_order if any(p.unit_type == u for p in points)]

    n = len(unit_types)
    ncols = min(3, n)
    nrows = int(np.ceil(n / ncols))

    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.titlesize": 12,
            "axes.labelsize": 11,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "legend.fontsize": 10,
        }
    )
    plt.style.use("seaborn-v0_8-whitegrid")

    fig, axes = plt.subplots(
        nrows=nrows,
        ncols=ncols,
        figsize=(6.6 * ncols, 5.6 * nrows),
        sharey=True,
    )
    axes_list: list[Axes] = list(np.array(axes).reshape(-1))

    all_x = np.array([p.sf for p in points])
    all_y = np.array([p.eff_psf for p in points])
    x_pad = max(25.0, 0.06 * (all_x.max() - all_x.min()))
    y_pad = max(0.05, 0.08 * (all_y.max() - all_y.min()))
    xlim = (max(0.0, float(all_x.min() - x_pad)), float(all_x.max() + x_pad))
    ylim = (max(0.0, float(all_y.min() - y_pad)), float(all_y.max() + y_pad))

    for idx, unit_type in enumerate(unit_types):
        ax = axes_list[idx]
        unit_points = [p for p in points if p.unit_type == unit_type]
        comps = [p for p in unit_points if not p.is_subject]
        subject = [p for p in unit_points if p.is_subject]

        color = _palette_for(unit_type)
        marker = _marker_for(unit_type)

        if comps:
            ax.scatter(
                [p.sf for p in comps],
                [p.eff_psf for p in comps],
                c=color,
                s=120,
                marker=marker,
                alpha=0.78,
                edgecolors="white",
                linewidths=0.8,
                label=f"{unit_type} Comps",
                zorder=3,
            )

        if subject:
            ax.scatter(
                [p.sf for p in subject],
                [p.eff_psf for p in subject],
                c="#e74c3c",
                s=190,
                marker=marker,
                alpha=0.98,
                edgecolors="#111111",
                linewidths=1.6,
                label="Subject",
                zorder=4,
            )

        # Annotations: label each point, but keep it light.
        for j, p in enumerate(unit_points):
            offset = (8, 8) if (j % 2 == 0) else (8, -12)
            ax.annotate(
                p.property_name,
                (p.sf, p.eff_psf),
                textcoords="offset points",
                xytext=offset,
                fontsize=9 if p.is_subject else 8,
                fontweight="bold" if p.is_subject else "normal",
                color="#e74c3c" if p.is_subject else "#2c3e50",
                alpha=0.95 if p.is_subject else 0.85,
                zorder=5,
            )

        # Trend line: prefer comps-only.
        fit_points = comps if len(comps) >= 2 else unit_points
        if len(fit_points) >= 2:
            x = np.array([p.sf for p in fit_points], dtype=float)
            y = np.array([p.eff_psf for p in fit_points], dtype=float)
            z = np.polyfit(x, y, 1)
            p_line = np.poly1d(z)
            x_line = np.linspace(x.min(), x.max(), 100)
            ax.plot(
                x_line,
                p_line(x_line),
                linestyle="--",
                color=color,
                alpha=0.45,
                linewidth=2.0,
                zorder=2,
            )

        ax.set_title(f"{unit_type}", fontweight="bold")
        ax.set_xlim(*xlim)
        ax.set_ylim(*ylim)
        ax.set_xlabel("Unit Size (SF)")
        if idx % ncols == 0:
            ax.set_ylabel("Effective Rent ($/SF)")

        _apply_axes_style(ax)

    # Hide unused axes.
    for ax in axes_list[len(unit_types) :]:
        ax.axis("off")

    suptitle = title
    if subtitle:
        suptitle = f"{title}\n{subtitle}"
    fig.suptitle(suptitle, fontsize=14, fontweight="bold", y=0.98)

    handles: list[Artist] = []
    labels: list[str] = []
    for ax in axes_list[: len(unit_types)]:
        handles_for_axis, labels_for_axis = ax.get_legend_handles_labels()
        for hh, ll in zip(handles_for_axis, labels_for_axis, strict=False):
            if ll not in labels:
                handles.append(hh)
                labels.append(ll)
    if handles:
        fig.legend(handles, labels, loc="lower center", ncol=min(3, len(labels)), frameon=False)

    fig.tight_layout(rect=(0, 0.06, 1, 0.94))

    out_png = out_base.with_suffix(".png")
    out_svg = out_base.with_suffix(".svg")
    fig.savefig(out_png, dpi=200, bbox_inches="tight")
    fig.savefig(out_svg, format="svg", bbox_inches="tight")
    plt.close(fig)


def _compute_trendline(points: list[RentPoint]) -> tuple[float, float] | None:
    if len(points) < 2:
        return None
    x = np.array([p.sf for p in points], dtype=float)
    y = np.array([p.eff_psf for p in points], dtype=float)
    z = np.polyfit(x, y, 1)
    return float(z[0]), float(z[1])


def _build_interactive_payload(
    points: list[RentPoint], title: str, subtitle: str | None
) -> InteractivePayload:
    unit_order = ["Studio", "1BR", "2BR", "3BR"]
    unit_types = [u for u in unit_order if any(p.unit_type == u for p in points)]

    all_x = np.array([p.sf for p in points], dtype=float)
    all_y = np.array([p.eff_psf for p in points], dtype=float)
    x_pad = max(25.0, 0.06 * (all_x.max() - all_x.min()))
    y_pad = max(0.05, 0.08 * (all_y.max() - all_y.min()))
    xlim = [max(0.0, float(all_x.min() - x_pad)), float(all_x.max() + x_pad)]
    ylim = [max(0.0, float(all_y.min() - y_pad)), float(all_y.max() + y_pad)]

    series: list[_SeriesPayload] = []
    for unit_type in unit_types:
        unit_points = [p for p in points if p.unit_type == unit_type]
        comps = [p for p in unit_points if not p.is_subject]
        subject = [p for p in unit_points if p.is_subject]

        trend_points = comps if len(comps) >= 2 else unit_points
        trend = _compute_trendline(trend_points)
        points_payload: list[_PointPayload] = [
            {
                "property": p.property_name,
                "sf": p.sf,
                "eff_psf": p.eff_psf,
                "is_subject": p.is_subject,
            }
            for p in unit_points
        ]
        series.append(
            {
                "unit_type": unit_type,
                "color": _palette_for(unit_type),
                "marker": _marker_for(unit_type),
                "points": points_payload,
                "trend": {"m": trend[0], "b": trend[1]} if trend else None,
                "subject_name": subject[0].property_name if subject else None,
            }
        )

    return {
        "title": title,
        "subtitle": subtitle,
        "xlim": xlim,
        "ylim": ylim,
        "series": series,
        "axis": {"x": "Unit Size (SF)", "y": "Effective Rent ($/SF)"},
        "note": "Hover for details. Subject points are highlighted in red.",
    }


def write_interactive_html_inline_svg(payload: InteractivePayload, out_path: Path) -> None:
    """
    Write a self-contained HTML file (no external JS/CSS) rendering an interactive SVG scatter plot.
    """
    data_json = json.dumps(payload, separators=(",", ":"))
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{payload["title"]} — $/SF Scatter</title>
  <style>
    :root {{
      --bg: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --grid: rgba(15, 23, 42, 0.10);
      --card: #ffffff;
      --border: rgba(15, 23, 42, 0.10);
      --shadow: 0 10px 30px rgba(2, 6, 23, 0.08);
      --subject: #e74c3c;
      --chip: rgba(15, 23, 42, 0.06);
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica,
        Arial, "Apple Color Emoji","Segoe UI Emoji";
      color: var(--text);
      background: radial-gradient(1200px 600px at 20% 0%, rgba(99,102,241,0.10), transparent 60%),
                  radial-gradient(900px 500px at 90% 20%, rgba(34,197,94,0.10), transparent 55%),
                  var(--bg);
    }}
    .wrap {{
      max-width: 1080px;
      margin: 28px auto;
      padding: 0 16px 28px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow);
      padding: 18px 18px 12px;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 800;
      letter-spacing: -0.02em;
    }}
    .sub {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      margin-top: 12px;
      margin-bottom: 10px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border-radius: 999px;
      background: var(--chip);
      border: 1px solid rgba(15, 23, 42, 0.08);
      font-size: 12px;
      user-select: none;
    }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 999px;
      display: inline-block;
    }}
    .hint {{
      margin-left: auto;
      color: var(--muted);
      font-size: 12px;
    }}
    .chart {{
      position: relative;
    }}
    #tooltip {{
      position: absolute;
      pointer-events: none;
      opacity: 0;
      transform: translate(-50%, -100%);
      padding: 10px 10px 9px;
      border-radius: 12px;
      background: rgba(15, 23, 42, 0.92);
      color: white;
      box-shadow: 0 14px 40px rgba(2, 6, 23, 0.35);
      font-size: 12px;
      line-height: 1.3;
      max-width: 280px;
      transition: opacity 120ms ease;
      z-index: 10;
      backdrop-filter: blur(10px);
    }}
    #tooltip .k {{
      font-weight: 700;
      margin-bottom: 4px;
    }}
    svg {{
      width: 100%;
      height: auto;
      display: block;
      border-radius: 14px;
    }}
    .foot {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono",
        "Courier New", monospace;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1 id="title"></h1>
      <div class="sub" id="subtitle"></div>
      <div class="controls" id="controls"></div>
      <div class="chart">
        <div id="tooltip"></div>
        <svg id="svg" viewBox="0 0 980 620" role="img" aria-label="$/SF scatter plot"></svg>
      </div>
      <div class="foot" id="foot"></div>
    </div>
  </div>

  <script>
  const DATA = {data_json};

  const $ = (sel) => document.querySelector(sel);
  const titleEl = $("#title");
  const subtitleEl = $("#subtitle");
  const controlsEl = $("#controls");
  const svg = $("#svg");
  const tooltip = $("#tooltip");

  titleEl.textContent = DATA.title + ": Competitive Positioning ($/SF vs Unit Size)";
  subtitleEl.textContent = DATA.subtitle || "";
  $("#foot").textContent = DATA.note || "";

  const WIDTH = 980, HEIGHT = 620;
  const M = {{ left: 86, right: 22, top: 28, bottom: 72 }};
  const IW = WIDTH - M.left - M.right;
  const IH = HEIGHT - M.top - M.bottom;

  const xMin = DATA.xlim[0], xMax = DATA.xlim[1];
  const yMin = DATA.ylim[0], yMax = DATA.ylim[1];

  const xScale = (x) => M.left + ( (x - xMin) / (xMax - xMin) ) * IW;
  const yScale = (y) => M.top + (1 - ( (y - yMin) / (yMax - yMin) )) * IH;

  const fmtMoney = (v) => "$" + v.toFixed(2);
  const fmtInt = (v) => Math.round(v).toString();

  const make = (name, attrs = {{}}) => {{
    const el = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [k,v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }};

  const markerPath = (kind, size) => {{
    // Centered at 0,0
    const s = size;
    if (kind === "^") return `M 0 ${{-s}} L ${{s}} ${{s}} L ${{-s}} ${{s}} Z`;
    if (kind === "s")
      return `M ${{-s}} ${{-s}} L ${{s}} ${{-s}} L ${{s}} ${{s}} L ${{-s}} ${{s}} Z`;
    if (kind === "D") return `M 0 ${{-s}} L ${{s}} 0 L 0 ${{s}} L ${{-s}} 0 Z`;
    return `M 0 0 m -${{s}}, 0 a ${{s}},${{s}} 0 1,0 ${{2*s}},0 a ${{s}},${{s}} 0 1,0 -${{2*s}},0`;
  }};

  const state = {{
    visible: {{}},
  }};

  for (const s of DATA.series) state.visible[s.unit_type] = true;

  const renderControls = () => {{
    controlsEl.innerHTML = "";
    for (const s of DATA.series) {{
      const label = document.createElement("label");
      label.className = "chip";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.visible[s.unit_type];
      cb.addEventListener("change", () => {{
        state.visible[s.unit_type] = cb.checked;
        draw();
      }});
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = s.color;
      const text = document.createElement("span");
      text.textContent = s.unit_type;
      label.appendChild(cb);
      label.appendChild(dot);
      label.appendChild(text);
      controlsEl.appendChild(label);
    }}
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "Hover points for details";
    controlsEl.appendChild(hint);
  }};

  const drawAxes = () => {{
    // Background
    svg.appendChild(
      make("rect", {{ x: 0, y: 0, width: WIDTH, height: HEIGHT, fill: "#ffffff", rx: 14, ry: 14 }})
    );

    // Gridlines
    const gx = make("g", {{ stroke: "var(--grid)", "stroke-width": "1" }});
    const gy = make("g", {{ stroke: "var(--grid)", "stroke-width": "1" }});

    const xTicks = 7;
    const yTicks = 6;
    for (let i = 0; i <= xTicks; i++) {{
      const t = i / xTicks;
      const x = M.left + t * IW;
      gx.appendChild(make("line", {{ x1: x, y1: M.top, x2: x, y2: M.top + IH }}));
    }}
    for (let i = 0; i <= yTicks; i++) {{
      const t = i / yTicks;
      const y = M.top + t * IH;
      gy.appendChild(make("line", {{ x1: M.left, y1: y, x2: M.left + IW, y2: y }}));
    }}
    svg.appendChild(gx);
    svg.appendChild(gy);

    // Axes
    svg.appendChild(
      make("line", {{
        x1: M.left, y1: M.top + IH, x2: M.left + IW, y2: M.top + IH,
        stroke: "#0f172a", "stroke-width": "1.5"
      }})
    );
    svg.appendChild(
      make("line", {{
        x1: M.left, y1: M.top, x2: M.left, y2: M.top + IH,
        stroke: "#0f172a", "stroke-width": "1.5"
      }})
    );

    // Tick labels
    const labelStyle = {{ fill: "#475569", "font-size": "12px" }};
    for (let i = 0; i <= xTicks; i++) {{
      const t = i / xTicks;
      const x = M.left + t * IW;
      const v = xMin + t * (xMax - xMin);
      const text = make(
        "text",
        {{ x, y: M.top + IH + 22, "text-anchor": "middle", ...labelStyle }}
      );
      text.textContent = fmtInt(v);
      svg.appendChild(text);
    }}
    for (let i = 0; i <= yTicks; i++) {{
      const t = i / yTicks;
      const y = M.top + (1 - t) * IH;
      const v = yMin + t * (yMax - yMin);
      const text = make(
        "text",
        {{ x: M.left - 10, y: y + 4, "text-anchor": "end", ...labelStyle }}
      );
      text.textContent = fmtMoney(v);
      svg.appendChild(text);
    }}

    // Axis titles
    const xLab = make("text", {{
      x: M.left + IW / 2, y: HEIGHT - 30, "text-anchor": "middle",
      fill: "#0f172a", "font-size": "13px", "font-weight": "700"
    }});
    xLab.textContent = DATA.axis.x;
    svg.appendChild(xLab);

    const yLab = make("text", {{
      x: 22, y: M.top + IH / 2,
      transform: `rotate(-90 22 ${{M.top + IH/2}})`,
      "text-anchor": "middle",
      fill: "#0f172a",
      "font-size": "13px",
      "font-weight": "700"
    }});
    yLab.textContent = DATA.axis.y;
    svg.appendChild(yLab);
  }};

  const showTooltip = (evt, p) => {{
    const rect = svg.getBoundingClientRect();
    const x = evt.clientX - rect.left;
    const y = evt.clientY - rect.top;
    tooltip.style.left = `${{x}}px`;
    tooltip.style.top = `${{y}}px`;
    tooltip.style.opacity = "1";
    tooltip.innerHTML = `
      <div class="k">${{p.property}}</div>
      <div><span class="mono">${{p.unit_type}}</span> • ${{Math.round(p.sf)}} SF</div>
      <div>Eff $/SF: <b>${{fmtMoney(p.eff_psf)}}</b>
        ${{p.is_subject ? " • <b>Subject</b>" : ""}}</div>
    `;
  }};

  const hideTooltip = () => {{
    tooltip.style.opacity = "0";
  }};

  const drawSeries = () => {{
    const g = make("g", {{}});

    for (const s of DATA.series) {{
      if (!state.visible[s.unit_type]) continue;

      // Trend line (dashed)
      if (s.trend) {{
        const x1 = xMin, x2 = xMax;
        const y1 = s.trend.m * x1 + s.trend.b;
        const y2 = s.trend.m * x2 + s.trend.b;
        const line = make("line", {{
          x1: xScale(x1), y1: yScale(y1),
          x2: xScale(x2), y2: yScale(y2),
          stroke: s.color,
          "stroke-width": "2",
          "stroke-dasharray": "6 6",
          opacity: "0.35"
        }});
        g.appendChild(line);
      }}

      for (const p of s.points) {{
        const cx = xScale(p.sf);
        const cy = yScale(p.eff_psf);
        const size = p.is_subject ? 7.5 : 6;

        const shape = make("path", {{
          d: markerPath(s.marker, size),
          transform: `translate(${{cx}} ${{cy}})`,
          fill: p.is_subject ? "var(--subject)" : s.color,
          opacity: p.is_subject ? "0.98" : "0.78",
          stroke: p.is_subject ? "#111111" : "#ffffff",
          "stroke-width": p.is_subject ? "1.6" : "0.8",
          cursor: "pointer",
        }});
        shape.addEventListener(
          "mousemove",
          (evt) => showTooltip(evt, {{...p, unit_type: s.unit_type}})
        );
        shape.addEventListener("mouseleave", hideTooltip);
        g.appendChild(shape);

        // Always label the subject.
        if (p.is_subject) {{
          const t = make("text", {{
            x: cx + 10,
            y: cy - 10,
            fill: "var(--subject)",
            "font-size": "12px",
            "font-weight": "800",
          }});
          t.textContent = "SUBJECT";
          g.appendChild(t);
        }}
      }}
    }}

    svg.appendChild(g);
  }};

  const draw = () => {{
    svg.innerHTML = "";
    drawAxes();
    drawSeries();
  }};

  renderControls();
  draw();
  </script>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")


def write_interactive_html_plotly_cdn(payload: InteractivePayload, out_path: Path) -> None:
    """
    Write a tiny HTML file that uses Plotly from a CDN (not self-contained).
    """
    data_json = json.dumps(payload, separators=(",", ":"))
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{payload["title"]} — $/SF Scatter (Plotly)</title>
  <style>
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      background: #f8fafc;
      color: #0f172a;
    }}
    .wrap {{ max-width: 1100px; margin: 22px auto; padding: 0 14px 22px; }}
    .card {{
      background: white;
      border: 1px solid rgba(15,23,42,0.10);
      border-radius: 14px;
      padding: 14px 14px 8px;
      box-shadow: 0 10px 30px rgba(2,6,23,0.08);
    }}
    h1 {{ margin: 0; font-size: 18px; font-weight: 800; letter-spacing: -0.02em; }}
    .sub {{ margin-top: 6px; color: #475569; font-size: 13px; }}
    #plot {{ width: 100%; height: 640px; }}
    .foot {{ margin-top: 8px; color: #64748b; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1 id="t"></h1>
      <div class="sub" id="s"></div>
      <div id="plot"></div>
      <div class="foot">{payload.get("note","")}</div>
    </div>
  </div>

  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <script>
    const DATA = {data_json};
    document.getElementById("t").textContent =
      DATA.title + ": Competitive Positioning ($/SF vs Unit Size)";
    document.getElementById("s").textContent = DATA.subtitle || "";

    const traces = [];
    for (const s of DATA.series) {{
      const compX = [], compY = [], compText = [];
      const subjX = [], subjY = [], subjText = [];
      for (const p of s.points) {{
        const t =
          `${{p.property}}<br>${{s.unit_type}} • ${{Math.round(p.sf)}} SF<br>` +
          `Eff $/SF: ${{p.eff_psf.toFixed(2)}}` +
          (p.is_subject ? "<br><b>Subject</b>" : "");
        if (p.is_subject) {{ subjX.push(p.sf); subjY.push(p.eff_psf); subjText.push(t); }}
        else {{ compX.push(p.sf); compY.push(p.eff_psf); compText.push(t); }}
      }}
      traces.push({{
        type: "scatter",
        mode: "markers",
        name: `${{s.unit_type}} Comps`,
        x: compX,
        y: compY,
        text: compText,
        hoverinfo: "text",
        marker: {{
          color: s.color,
          size: 11,
          symbol: s.marker === "o"
            ? "circle"
            : (s.marker === "^"
              ? "triangle-up"
              : (s.marker === "s" ? "square" : "diamond")),
          opacity: 0.78,
          line: {{ color: "white", width: 1 }},
        }},
      }});
      if (subjX.length) {{
        traces.push({{
          type: "scatter",
          mode: "markers",
          name: `${{s.unit_type}} Subject`,
          x: subjX,
          y: subjY,
          text: subjText,
          hoverinfo: "text",
          marker: {{
            color: "#e74c3c",
            size: 14,
            symbol: s.marker === "o"
              ? "circle"
              : (s.marker === "^"
                ? "triangle-up"
                : (s.marker === "s" ? "square" : "diamond")),
            opacity: 0.98,
            line: {{ color: "#111111", width: 2 }},
          }},
        }});
      }}
      if (s.trend) {{
        const x1 = DATA.xlim[0], x2 = DATA.xlim[1];
        const y1 = s.trend.m * x1 + s.trend.b;
        const y2 = s.trend.m * x2 + s.trend.b;
        traces.push({{
          type: "scatter",
          mode: "lines",
          name: `${{s.unit_type}} Trend`,
          x: [x1, x2],
          y: [y1, y2],
          line: {{ color: s.color, width: 2, dash: "dash" }},
          opacity: 0.35,
          hoverinfo: "skip",
          showlegend: false,
        }});
      }}
    }}

    const layout = {{
      paper_bgcolor: "white",
      plot_bgcolor: "white",
      margin: {{ l: 70, r: 20, t: 10, b: 60 }},
      xaxis: {{
        title: DATA.axis.x,
        range: DATA.xlim,
        gridcolor: "rgba(15,23,42,0.10)",
        zeroline: false,
      }},
      yaxis: {{
        title: DATA.axis.y,
        range: DATA.ylim,
        gridcolor: "rgba(15,23,42,0.10)",
        zeroline: false,
      }},
      legend: {{ orientation: "h", y: -0.20 }},
      hoverlabel: {{ bgcolor: "rgba(15,23,42,0.92)" }},
    }};

    Plotly.newPlot("plot", traces, layout, {{ responsive: true, displaylogo: false }});
  </script>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate rent $/SF scatterplots from comp analysis markdown."
    )
    parser.add_argument("--md", type=Path, required=True, help="Path to comp analysis .md file.")
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Output directory (defaults to the markdown file's directory).",
    )
    parser.add_argument(
        "--basename",
        type=str,
        default="rent_sf_scatter_plot",
        help="Output filename base (no extension).",
    )
    parser.add_argument(
        "--html",
        choices=["none", "inline", "plotly-cdn", "both"],
        default="both",
        help=(
            "Also write interactive HTML output (inline is self-contained; plotly-cdn is smallest "
            "but needs internet)."
        ),
    )
    args = parser.parse_args()

    md_path: Path = args.md
    if not md_path.exists():
        raise FileNotFoundError(str(md_path))

    outdir = args.outdir or md_path.parent
    outdir.mkdir(parents=True, exist_ok=True)
    out_base = outdir / args.basename

    md_text = md_path.read_text(encoding="utf-8")
    prop_name, as_of = _extract_metadata(md_text)
    title = prop_name or md_path.stem
    subtitle = None
    if as_of:
        subtitle = f"Competitive Positioning — As of {as_of}"

    points = extract_rent_points(md_path)
    plot_rent_sf(points, title=title, subtitle=subtitle, out_base=out_base)
    print(f"Saved: {out_base.with_suffix('.png')}")
    print(f"Saved: {out_base.with_suffix('.svg')}")

    payload = _build_interactive_payload(points, title=title, subtitle=subtitle)
    if args.html in {"inline", "both"}:
        out_html = out_base.with_name(f"{out_base.name}_interactive.html")
        write_interactive_html_inline_svg(payload, out_html)
        print(f"Saved: {out_html}")
    if args.html in {"plotly-cdn", "both"}:
        out_html = out_base.with_name(f"{out_base.name}_plotly_cdn.html")
        write_interactive_html_plotly_cdn(payload, out_html)
        print(f"Saved: {out_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
