"""Render an Investment Committee memo from forecast diagnostics + ensemble.

Inputs:
  - model_diagnostics_<tier>.json (per-model + ensemble metrics)
  - v2_ensemble_<tier>.csv (8-quarter forecast with bands)
  - property config YAML (for subject property metadata)
  - optional cross-references: comp analysis MD, supply analysis MD,
    rent roll analysis MD (if on disk)

Output:
  - reports/<metro>/<property>/forecasts/forecast_memo_<tier>.md (and .html)

The memo follows the .claude/skills/forecast/SKILL.md output format and
adds a Model Diagnostics section listing per-model RMSE / MAE / bias /
residual ACF / Ljung-Box / CI coverage / ensemble weight / plain-English
strengths and weaknesses.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import yaml


def _fmt_pct(x: float, plus: bool = False) -> str:
    if x is None or (isinstance(x, float) and (x != x)):  # NaN
        return "—"
    sign = "+" if plus and x >= 0 else ""
    return f"{sign}{x*100:.2f}%"


def render_memo(
    *,
    diagnostics_json: Path,
    ensemble_csv: Path,
    property_config: Path | str,
    out_md: Path,
    supply_analysis_md: Path | None = None,
    comp_analysis_md: Path | None = None,
    rent_roll_md: Path | None = None,
) -> Path:
    diag = json.loads(Path(diagnostics_json).read_text())
    ens = pd.read_csv(ensemble_csv)
    cfg = yaml.safe_load(Path(property_config).read_text())
    subject = cfg["notes"]["subject_property"]

    meta = diag["meta"]
    models = diag["models"]
    ensemble = diag["ensemble"]

    lines: list[str] = []
    lines.append(f"# Market Forecast: {meta.get('property')} — {meta.get('tier')}")
    lines.append("")
    lines.append(f"**Property:** {subject['name']}  ")
    lines.append(f"**Address:** {subject.get('address', '—')}  ")
    lines.append(f"**Submarket Tier:** {meta.get('tier')}  ")
    lines.append(f"**Forecast Horizon:** {meta.get('forecast_horizon')}Q ({meta.get('forecast_horizon', 0) // 4}Y)  ")  # noqa: E501
    lines.append(f"**Confidence Level:** {ensemble.get('confidence_level')}  ")
    lines.append(f"**Panel:** {meta.get('panel_quarters')}Q ({meta.get('panel_start')} → {meta.get('panel_end')})  ")  # noqa: E501
    lines.append("")
    lines.append("---")
    lines.append("")

    # Executive Summary
    lines.append("## Executive Summary")
    lines.append("")
    base_y1 = ens["base"].iloc[:4].mean()
    base_y2 = ens["base"].iloc[4:8].mean() if len(ens) >= 8 else float("nan")
    bear_y1 = ens["bear"].iloc[:4].mean()
    bull_y1 = ens["bull"].iloc[:4].mean()
    lines.append(
        f"The {meta.get('tier')} ensemble ({len(models) - 1} non-naive models, RMSE-inverse-weighted) "  # noqa: E501
        f"projects average YoY rent growth of **{_fmt_pct(base_y1, plus=True)}** in Y1 and "
        f"**{_fmt_pct(base_y2, plus=True)}** in Y2 under the base case. "
        f"Scenario bands span **{_fmt_pct(bear_y1, plus=True)}** (bear) to "
        f"**{_fmt_pct(bull_y1, plus=True)}** (bull) in Y1. Ensemble backtest RMSE: "
        f"**{_fmt_pct(ensemble['ensemble_rmse'])}** vs historical YoY std-dev "
        f"**{_fmt_pct(ensemble['yoy_std'])}** → confidence **{ensemble['confidence_level']}**."
    )
    if ensemble.get("regime_change_flag"):
        lines.append("")
        lines.append("> ⚠ **Regime change flag set** — recent 4Q YoY rent growth deviates >2σ "
                     "from the 5Y rolling mean. Treat point forecast with caution; widen bands.")
    lines.append("")

    # Section 1 — Vacancy (placeholder; lifted from supply analysis if present)
    lines.append("## 1. Vacancy Rate Forecast")
    lines.append("")
    if supply_analysis_md and Path(supply_analysis_md).exists():
        lines.append(f"_Cross-referencing `{supply_analysis_md.name}` for vacancy fundamentals._")
    lines.append("")
    lines.append("Refer to the supply analysis output for the vacancy-trajectory model. "
                 "This memo's primary deliverable is rent-growth forecasting; vacancy is a "
                 "co-modeled feature in BVAR / Ridge / ElasticNet / GBM but not the headline output.")  # noqa: E501
    lines.append("")

    # Section 2 — Rent Growth Forecast
    lines.append("## 2. Rent Growth Forecast")
    lines.append("")
    lines.append("### Quarterly Forecast — Base / Bull / Bear")
    lines.append("")
    lines.append("| Period | Base | Bull | Bear | 80% CI Lo | 80% CI Hi |")
    lines.append("|--------|-----:|-----:|-----:|----------:|----------:|")
    for _, row in ens.iterrows():
        lines.append(
            f"| {row['period']} | {_fmt_pct(row['base'], plus=True)} | "
            f"{_fmt_pct(row['bull'], plus=True)} | {_fmt_pct(row['bear'], plus=True)} | "
            f"{_fmt_pct(row['ci_80_lo'], plus=True)} | {_fmt_pct(row['ci_80_hi'], plus=True)} |"
        )
    lines.append("")
    lines.append("### Annual CAGR")
    if len(ens) >= 4:
        cagr_base_y1 = ens["base"].iloc[:4].mean()
        cagr_bull_y1 = ens["bull"].iloc[:4].mean()
        cagr_bear_y1 = ens["bear"].iloc[:4].mean()
        lines.append("")
        lines.append("| Horizon | Base | Bull | Bear |")
        lines.append("|---------|-----:|-----:|-----:|")
        lines.append(f"| Year 1 (Q1-Q4) | {_fmt_pct(cagr_base_y1, plus=True)} | "
                     f"{_fmt_pct(cagr_bull_y1, plus=True)} | {_fmt_pct(cagr_bear_y1, plus=True)} |")
        if len(ens) >= 8:
            lines.append(f"| Year 2 (Q5-Q8) | {_fmt_pct(ens['base'].iloc[4:8].mean(), plus=True)} | "  # noqa: E501
                         f"{_fmt_pct(ens['bull'].iloc[4:8].mean(), plus=True)} | "
                         f"{_fmt_pct(ens['bear'].iloc[4:8].mean(), plus=True)} |")
    lines.append("")

    # Section 3 — Supply Pipeline (cross-ref)
    lines.append("## 3. Supply Pipeline")
    lines.append("")
    if supply_analysis_md and Path(supply_analysis_md).exists():
        lines.append(f"See `{Path(supply_analysis_md).name}` (already produced by /supply skill).")
    lines.append("")

    # Section 4 — Demand Drivers (cross-ref)
    lines.append("## 4. Demand Drivers")
    lines.append("")
    lines.append("Co-modeled in BVAR / Ridge / ElasticNet via lagged absorption / vacancy / inventory. "  # noqa: E501
                 "Standalone demand-side narrative in the demographics & labor-shed report.")
    lines.append("")

    # Section 5 — Scenario Analysis
    lines.append("## 5. Scenario Analysis")
    lines.append("")
    lines.append("**Probability weighting** (analyst convention, not model-derived): Base 60% / Bull 20% / Bear 20%.")  # noqa: E501
    lines.append("")
    lines.append(f"- **Base** ({_fmt_pct(base_y1, plus=True)} Y1 / {_fmt_pct(base_y2, plus=True)} Y2): "  # noqa: E501
                 "ensemble point forecast — assumes panel dynamics persist.")
    lines.append(f"- **Bull** ({_fmt_pct(bull_y1, plus=True)} Y1): point + 1× backtest-RMSE per quarter, scaled by sqrt(h). "  # noqa: E501
                 "Equivalent to ~85th percentile of the historical residual distribution.")
    lines.append(f"- **Bear** ({_fmt_pct(bear_y1, plus=True)} Y1): symmetric ~15th-percentile band.")  # noqa: E501
    lines.append("")

    # Section 6 — Investment Implications (placeholder; analyst fills in)
    lines.append("## 6. Investment Implications")
    lines.append("")
    lines.append("_Property-level UW translation pending — apply the Y1-Y5 YoY path to the subject's "  # noqa: E501
                 "in-place rent and current vacancy to project NOI / IRR. See the rent roll analysis "  # noqa: E501
                 "for current-state in-place rents and the comp analysis for market positioning._")
    lines.append("")
    if rent_roll_md and Path(rent_roll_md).exists():
        lines.append(f"Cross-references: [`{Path(rent_roll_md).name}`]({rent_roll_md})")
    if comp_analysis_md and Path(comp_analysis_md).exists():
        lines.append(f", [`{Path(comp_analysis_md).name}`]({comp_analysis_md})")
    lines.append("")

    # Section 7 — Methodology
    lines.append("## 7. Methodology")
    lines.append("")
    lines.append("**Pipeline:** `etl/submarket_forecast.py` (6-model ensemble + RMSE-inverse weighting).")  # noqa: E501
    lines.append("")
    lines.append("**Models:** Naive (baseline), ARIMA (univariate), BVAR (multivariate Bayesian VAR), "  # noqa: E501
                 "Ridge (L2-regularized linear), GBM (gradient boosting, regularized), "
                 "ElasticNet (L1+L2 hybrid). Weights: inverse-RMSE / sum-of-inverse-RMSE; "
                 "Naive excluded from ensemble (used as relative baseline only).")
    lines.append("")
    lines.append("**CI methodology:** 80% bands derived from pooled in-sample residual std × 1.28 × √h. "  # noqa: E501
                 "Scenario (Bull/Bear) bands calibrated to backtest RMSE × √h — more conservative than "  # noqa: E501
                 "in-sample residuals because they reflect out-of-sample error.")
    lines.append("")

    # Model Diagnostics — NEW required section
    lines.append("## Model Diagnostics")
    lines.append("")
    lines.append("### Per-model statistical metrics")
    lines.append("")
    lines.append("| Model | RMSE | MAE | Bias | Resid ACF₁ | Ljung-Box p | 80% CI Cov | Weight |")
    lines.append("|:------|-----:|----:|-----:|-----------:|------------:|-----------:|-------:|")
    for m in ("Naive", "ARIMA", "BVAR", "Ridge", "GBM", "ElasticNet"):
        v = models.get(m, {})
        lines.append(
            f"| {m} | {_fmt_pct(v.get('rmse'))} | {_fmt_pct(v.get('mae'))} | "
            f"{_fmt_pct(v.get('bias'), plus=True)} | "
            f"{v.get('residual_acf_lag1', float('nan')):+.2f} | "
            f"{v.get('ljung_box_lag5_pvalue', float('nan')):.3f} | "
            f"{_fmt_pct(v.get('ci80_coverage'))} | "
            f"{v.get('ensemble_weight', 0):.2%} |"
        )
    lines.append("")
    lines.append("**Reading the table:**")
    lines.append("- **Bias** > 0 = model under-predicts (actual > predicted on average).")
    lines.append("- **Resid ACF₁** near 0 means residuals are white noise (good); large |ACF| signals model misspecification.")  # noqa: E501
    lines.append("- **Ljung-Box p** > 0.05 fails to reject white-noise residuals (good).")
    lines.append("- **80% CI Cov** should be ≈80%; lower = bands are too tight (under-confident in tails).")  # noqa: E501
    lines.append("- **Weight** = inverse-RMSE share; higher = model contributes more to the point forecast.")  # noqa: E501
    lines.append("")

    lines.append("### Ensemble metrics")
    lines.append("")
    lines.append(f"- **Ensemble RMSE (out-of-sample backtest):** {_fmt_pct(ensemble.get('ensemble_rmse'))}")  # noqa: E501
    cov = ensemble.get("ensemble_ci80_coverage")
    cov_str = _fmt_pct(cov) if isinstance(cov, (int, float)) and cov == cov else "—"
    lines.append(f"- **80% CI calibration:** {cov_str} of out-of-sample actuals fell inside the 80% band")  # noqa: E501
    lines.append(f"- **Confidence level:** {ensemble.get('confidence_level')} "
                 f"(ratio = ensemble RMSE / historical YoY σ = "
                 f"{(ensemble.get('ensemble_rmse', 0) / max(ensemble.get('yoy_std', 1e-9), 1e-9)):.2f})")  # noqa: E501
    if ensemble.get("regime_change_flag"):
        lines.append("- **⚠ Regime-change flag:** recent 4Q YoY > 2σ from 5Y rolling mean — model risk elevated")  # noqa: E501
    lines.append("")

    lines.append("### Per-model strengths & weaknesses")
    lines.append("")
    for m in ("Naive", "ARIMA", "BVAR", "Ridge", "GBM", "ElasticNet"):
        v = models.get(m, {})
        if v.get("strengths") or v.get("weaknesses"):
            lines.append(f"#### {m}")
            lines.append("")
            lines.append(f"_Weight in ensemble: **{v.get('ensemble_weight', 0):.2%}** | "
                         f"Backtest RMSE: **{_fmt_pct(v.get('rmse'))}**_")
            lines.append("")
            if v.get("strengths"):
                lines.append(f"**Strengths.** {v['strengths']}")
                lines.append("")
            if v.get("weaknesses"):
                lines.append(f"**Weaknesses.** {v['weaknesses']}")
                lines.append("")

    lines.append("### Data Quality Gates")
    lines.append("")
    gates = diag.get("data_quality_gates", {})
    for gate_name, status in gates.items():
        lines.append(f"- {gate_name}: {status}")
    lines.append("")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")
    return out_md


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Investment Committee forecast memo.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--tier-slug", required=True, help='e.g. "1_2_star", "4_5_star", "all"')
    parser.add_argument("--html", action="store_true", help="Also generate styled HTML")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    report_path = Path(cfg["outputs"]["report_path"])
    base_dir = report_path.parent
    forecasts_dir = base_dir / "forecasts"

    diagnostics_json = forecasts_dir / f"model_diagnostics_{args.tier_slug}.json"
    ensemble_csv = forecasts_dir / f"v2_ensemble_{args.tier_slug}.csv"

    # Optional cross-references — pass if they exist
    supply_md = base_dir / "supply_analysis.md"
    comp_md = next(iter(sorted((base_dir / "comps").glob("comp_analysis_*.md"))), None)
    rr_md = base_dir / "rent-roll" / "clean" / "rent_roll_analysis_detailed.md"

    out_md = forecasts_dir / f"forecast_memo_{args.tier_slug}.md"
    render_memo(
        diagnostics_json=diagnostics_json,
        ensemble_csv=ensemble_csv,
        property_config=args.config,
        out_md=out_md,
        supply_analysis_md=supply_md if supply_md.exists() else None,
        comp_analysis_md=comp_md,
        rent_roll_md=rr_md if rr_md.exists() else None,
    )
    print(f"Wrote: {out_md}")
    if args.html:
        from etl.md_to_styled_html import md_to_html
        out_html = out_md.with_suffix(".html")
        out_html.write_text(
            md_to_html(out_md.read_text(encoding="utf-8"), base_dir=out_md.parent),
            encoding="utf-8",
        )
        print(f"Wrote: {out_html}")


if __name__ == "__main__":
    main()
