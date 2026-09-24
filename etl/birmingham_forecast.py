"""Back-compat import shim — forwards to :mod:`etl.submarket_forecast`.

This module was renamed to ``submarket_forecast`` when the pipeline was
generalized to run on any submarket × star-rating tier. The shim is
preserved so any existing scripts / notebooks importing from the old
path continue to work; new code should import from ``submarket_forecast``
directly.
"""

from etl.submarket_forecast import *  # noqa: F401, F403
from etl.submarket_forecast import (  # noqa: F401
    ForecastContext,
    build_ensemble,
    compare_to_costar,
    context_from_config,
    fit_all_models,
    load_panel,
    rolling_backtest,
    run_all_backtests,
    run_diagnostics,
)
