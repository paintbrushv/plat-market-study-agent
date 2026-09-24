from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from etl.submarket_forecast import ForecastContext, context_from_config


def test_context_from_config_irving(tmp_path: Path) -> None:
    config = tmp_path / "irving.yaml"
    config.write_text(
        """
notes:
  metro_slug: irving_tx_example_garden
  subject_property:
    name: Devon on Northgate
outputs:
  report_path: reports/irving-tx/devon-on-northgate/market-study-{date}.md
costar_submarket:
  parquet: data/paid/costar-submarket-v1/dallas_fort_worth_tx_usa_irving.parquet
  primary_star_rating: "1 & 2 Star"
""",
        encoding="utf-8",
    )
    ctx = context_from_config(config, star_rating=None)  # default to primary
    assert ctx.metro_slug == "irving_tx_example_garden"
    assert ctx.property_name == "Devon on Northgate"
    assert ctx.star_rating == "1 & 2 Star"
    assert ctx.tier_slug == "1_2_star"
    assert str(ctx.parquet_path).endswith("dallas_fort_worth_tx_usa_irving.parquet")
    assert ctx.output_dir == Path("reports/irving-tx/devon-on-northgate/forecasts")


def test_context_from_config_with_explicit_star_rating(tmp_path: Path) -> None:
    """Explicit star_rating arg overrides costar.primary_star_rating."""
    config = tmp_path / "irving.yaml"
    config.write_text(
        """
notes:
  metro_slug: irving_tx_example_garden
  subject_property:
    name: Devon on Northgate
outputs:
  report_path: reports/irving-tx/devon-on-northgate/market-study-{date}.md
costar_submarket:
  parquet: data/paid/costar-submarket-v1/dallas_fort_worth_tx_usa_irving.parquet
  primary_star_rating: "1 & 2 Star"
""",
        encoding="utf-8",
    )
    ctx = context_from_config(config, star_rating="4 & 5 Star")
    assert ctx.star_rating == "4 & 5 Star"  # override takes precedence
    assert ctx.tier_slug == "4_5_star"
    # Other fields still derived correctly
    assert ctx.metro_slug == "irving_tx_example_garden"


def test_context_tier_slug_normalization() -> None:
    assert ForecastContext.normalize_tier_slug("4 & 5 Star") == "4_5_star"
    assert ForecastContext.normalize_tier_slug("1 & 2 Star") == "1_2_star"
    assert ForecastContext.normalize_tier_slug("All") == "all"
    assert ForecastContext.normalize_tier_slug("3 Star") == "3_star"


def test_load_panel_uses_ctx_parquet(tmp_path: Path) -> None:
    """Generic load_panel should read parquet from ctx, not module-level constant.

    The private repo ran this against a paid CoStar submarket parquet
    (data/paid/, never shipped). Here a synthetic parquet with the same schema
    exercises the same ctx-parquet contract.
    """
    import pandas as pd

    from etl.submarket_forecast import ForecastContext, load_panel

    n_q = 48
    df = pd.DataFrame({
        "date": [f"{2014 + i // 4} Q{(i % 4) + 1}" for i in range(n_q)],
        "star_rating": ["1 & 2 Star"] * n_q,
        "is_forecast": [False] * n_q,
        "effective_rent_growth_yoy": [0.02] * n_q,
        "vacancy_rate": [0.07] * n_q,
        "deliveries_12mo": [0] * n_q,
        "absorption_units_12mo": [0] * n_q,
        "under_construction": [0] * n_q,
        "inventory_units": [1000] * n_q,
        "effective_rent_unit": [1200.0 + i for i in range(n_q)],
    })
    parquet_path = tmp_path / "synthetic_submarket.parquet"
    df.to_parquet(parquet_path)

    ctx = ForecastContext(
        metro_slug="irving_tx_example_garden",
        property_name="Devon on Northgate",
        parquet_path=parquet_path,
        star_rating="1 & 2 Star",
        tier_slug="1_2_star",
        output_dir=tmp_path,
    )
    hist, fc = load_panel(ctx)
    assert "rent_growth_yoy" in hist.columns
    assert "vacancy_rate" in hist.columns


def test_load_panel_rejects_short_history(tmp_path: Path) -> None:
    """Panels with <40Q of history must raise ValueError."""
    from etl.submarket_forecast import ForecastContext, load_panel

    rows = []
    for i in range(20):
        rows.append({
            "date": f"{2020 + i // 4} Q{(i % 4) + 1}",
            "star_rating": "1 & 2 Star",
            "is_forecast": False,
            "effective_rent_growth_yoy": 0.02,
            "vacancy_rate": 0.07,
            "deliveries_12mo": 0,
            "absorption_units_12mo": 0,
            "under_construction": 0,
            "inventory_units": 1000,
            "effective_rent_unit": 1200,
        })
    df = pd.DataFrame(rows)
    p = tmp_path / "tiny.parquet"
    df.to_parquet(p)
    ctx = ForecastContext(
        metro_slug="x", property_name="x", parquet_path=p, star_rating="1 & 2 Star",
        tier_slug="1_2_star", output_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="Insufficient history"):
        load_panel(ctx)


def test_load_panel_rejects_implausible_vacancy(tmp_path: Path) -> None:
    """Vacancy rate > 50% triggers sanity gate."""
    from etl.submarket_forecast import ForecastContext, load_panel

    rows = []
    for i in range(50):
        rows.append({
            "date": f"{2010 + i // 4} Q{(i % 4) + 1}",
            "star_rating": "1 & 2 Star",
            "is_forecast": False,
            "effective_rent_growth_yoy": 0.02,
            "vacancy_rate": 0.07 if i < 49 else 0.65,
            "deliveries_12mo": 0,
            "absorption_units_12mo": 0,
            "under_construction": 0,
            "inventory_units": 1000,
            "effective_rent_unit": 1200,
        })
    p = tmp_path / "bad.parquet"
    pd.DataFrame(rows).to_parquet(p)
    ctx = ForecastContext(
        metro_slug="x", property_name="x", parquet_path=p, star_rating="1 & 2 Star",
        tier_slug="1_2_star", output_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="Vacancy out of plausible range"):
        load_panel(ctx)


def test_run_pipeline_writes_to_ctx_output_dir(tmp_path: Path) -> None:
    """run_pipeline writes outputs to ctx.output_dir, not module-level OUTPUT_DIR."""
    import numpy as np
    from etl.submarket_forecast import ForecastContext, run_pipeline

    # Build a synthetic parquet large enough to pass data-quality gates (>=40Q)
    rng = np.random.default_rng(42)
    n = 60
    rows = []
    for i in range(n):
        rows.append({
            "date": f"{2005 + i // 4} Q{(i % 4) + 1}",
            "star_rating": "4 & 5 Star",
            "is_forecast": False,
            "effective_rent_growth_yoy": float(rng.normal(0.025, 0.015)),
            "vacancy_rate": float(rng.uniform(0.06, 0.12)),
            "deliveries_12mo": int(rng.integers(0, 50)),
            "absorption_units_12mo": int(rng.integers(0, 50)),
            "under_construction": int(rng.integers(0, 100)),
            "inventory_units": 2000,
            "effective_rent_unit": 1200 + i * 2,
        })
    parquet_path = tmp_path / "synthetic.parquet"
    pd.DataFrame(rows).to_parquet(parquet_path)

    out = tmp_path / "forecasts"
    out.mkdir()
    ctx = ForecastContext(
        metro_slug="birmingham_al_example_highrise",
        property_name="The Example Highrise",
        parquet_path=parquet_path,
        star_rating="4 & 5 Star",
        tier_slug="4_5_star",
        output_dir=out,
        forecast_horizon=4,  # short horizon for fast test
    )
    result = run_pipeline(ctx)
    assert (out / "v2_forecast_4_5_star.csv").exists()
    assert (out / "v2_ensemble_4_5_star.csv").exists()
    assert any(out.glob("v2_backtest_*_4_5_star.csv"))
    assert "ensemble" in result
    assert "panel" in result


def test_birmingham_shim_reexports() -> None:
    """The old import path must continue to work for back-compat."""
    from etl.birmingham_forecast import (
        ForecastContext,
    )

    # Smoke-test: ForecastContext from the shim is the same class as the canonical
    from etl.submarket_forecast import ForecastContext as Canonical
    assert ForecastContext is Canonical


def test_run_pipeline_emits_diagnostics_json(tmp_path: Path) -> None:
    """run_pipeline writes model_diagnostics_<tier>.json alongside CSVs."""
    from etl.submarket_forecast import ForecastContext, run_pipeline

    # Build a synthetic CoStar-shaped parquet (matches the existing
    # test_run_pipeline_writes_to_ctx_output_dir pattern from Task 5).
    rows = []
    for i in range(60):
        rows.append({
            "date": f"{2010 + i // 4} Q{(i % 4) + 1}",
            "star_rating": "4 & 5 Star",
            "is_forecast": False,
            "effective_rent_growth_yoy": 0.02 + 0.005 * np.sin(i / 4),
            "vacancy_rate": 0.07 + 0.01 * np.cos(i / 6),
            "deliveries_12mo": 0,
            "absorption_units_12mo": 0,
            "under_construction": 0,
            "inventory_units": 1000,
            "effective_rent_unit": 1200,
        })
    p = tmp_path / "synth.parquet"
    pd.DataFrame(rows).to_parquet(p)

    out = tmp_path / "forecasts"
    ctx = ForecastContext(
        metro_slug="test_metro",
        property_name="Test",
        parquet_path=p,
        star_rating="4 & 5 Star",
        tier_slug="4_5_star",
        output_dir=out,
        forecast_horizon=4,
    )
    run_pipeline(ctx)
    diag_path = out / "model_diagnostics_4_5_star.json"
    assert diag_path.exists()
    import json
    payload = json.loads(diag_path.read_text())
    assert "models" in payload and len(payload["models"]) == 6
    assert "ensemble" in payload
    assert payload["ensemble"]["confidence_level"] in ("High", "Medium", "Low")
    # Verify all 6 model names present
    for m in ("Naive", "ARIMA", "BVAR", "Ridge", "GBM", "ElasticNet"):
        assert m in payload["models"]


def test_cli_invocation_runs_birmingham(tmp_path: Path) -> None:
    """Invoking the module with --config + --star-rating runs the full pipeline."""
    import subprocess
    config = "agents/configs/birmingham_al_example_highrise.yaml"
    if not Path(config).exists():
        pytest.skip("Birmingham config not present")
    parquet = Path("data/paid/costar-submarket/birmingham_al_usa_downtown_birmingham.parquet")
    if not parquet.exists():
        pytest.skip("Birmingham CoStar parquet not present (paid data)")
    out_dir = tmp_path / "forecasts"
    proc = subprocess.run(
        [
            "uv", "run", "python", "-m", "etl.submarket_forecast",
            "--config", config,
            "--star-rating", "4 & 5 Star",
            "--output-dir-override", str(out_dir),
            "--horizon", "4",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert (out_dir / "v2_forecast_4_5_star.csv").exists()
    assert (out_dir / "v2_ensemble_4_5_star.csv").exists()


def test_also_run_all_writes_cross_tier_csv(tmp_path: Path) -> None:
    """write_cross_tier_csv emits side-by-side primary vs All ensemble comparison."""
    from etl.submarket_forecast import (
        ForecastContext,
        run_pipeline,
        write_cross_tier_csv,
    )
    irving = Path("data/paid/costar-submarket-v1/dallas_fort_worth_tx_usa_irving.parquet")
    if not irving.exists():
        pytest.skip("Irving CoStar parquet not present")
    out = tmp_path / "forecasts"
    ctx_p = ForecastContext(
        metro_slug="m", property_name="P", parquet_path=irving,
        star_rating="1 & 2 Star", tier_slug="1_2_star", output_dir=out, forecast_horizon=4,
    )
    ctx_a = ForecastContext(
        metro_slug="m", property_name="P", parquet_path=irving,
        star_rating="All", tier_slug="all", output_dir=out, forecast_horizon=4,
    )
    r_p = run_pipeline(ctx_p)
    r_a = run_pipeline(ctx_a)
    write_cross_tier_csv(ctx_p, ctx_a, r_p, r_a)
    cross = out / "cross_tier_comparison.csv"
    assert cross.exists()
    df = pd.read_csv(cross)
    assert {"period", "primary_yoy", "all_yoy", "spread"}.issubset(df.columns)


def test_rolling_backtest_emits_ci_columns(tmp_path: Path) -> None:
    """rolling_backtest result includes pred_lo / pred_hi for downstream CI calibration."""
    from etl.submarket_forecast import (
        ForecastContext,
        NaiveModel,
        load_panel,
        rolling_backtest,
    )

    irving = Path("data/paid/costar-submarket-v1/dallas_fort_worth_tx_usa_irving.parquet")
    if not irving.exists():
        pytest.skip("Irving CoStar parquet not present")
    ctx = ForecastContext(
        metro_slug="x",
        property_name="x",
        parquet_path=irving,
        star_rating="1 & 2 Star",
        tier_slug="1_2_star",
        output_dir=tmp_path,
        backtest_min_train=80,  # short test set; ~46 backtest obs
    )
    panel, _ = load_panel(ctx)
    bt = rolling_backtest(ctx, panel, NaiveModel)
    assert "pred_lo" in bt.columns
    assert "pred_hi" in bt.columns
    # Bands should bracket the prediction (pred_lo <= predicted <= pred_hi where defined)
    nonnull = bt.dropna(subset=["pred_lo", "pred_hi"])
    assert (nonnull["pred_lo"] <= nonnull["predicted"]).all()
    assert (nonnull["predicted"] <= nonnull["pred_hi"]).all()
