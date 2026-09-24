# Market Study Agent

The Market Study Agent produces expert-level multifamily market studies with strict separation between public and licensed content. It bundles prompting assets, ETL pipelines, retrieval policies, and reporting templates into an auditable workflow.

## Repository Highlights
- Clear guardrails for public vs. paid data with policies, registries, and automated checks.
- Reusable agent prompts, report templates, and metro configs for rapid deployment.
- ETL modules and validation utilities to normalize inputs before analysis.
- CI pipelines that enforce compliance, provenance, and schema quality.

## Quickstart
1. Clone the repo and run `make setup` to install dependencies and pre-commit hooks.
2. Copy `.env.example` to `.env` and populate API keys, vendor credentials, and storage URLs.
3. Review `agents/configs/austin_tx.yaml` and adjust sources or outputs for your environment.
4. Pull public data with `make pull-public`, normalize it with `make normalize`, and build retrieval indices via `make index`.
5. Generate a sample report: `make report`. Review the output in `reports/published/` and the provenance log in `reports/logs/`.

## Adding a New Metro
- Duplicate `agents/configs/template.yaml`, update metro metadata, and map each metric to a dataset ID from `SOURCE_REGISTRY.csv`.
- Add any metro-specific ETL or lookup files under `data/public/processed/` or `data/registry/`.
- Extend tests if the metro introduces new validation cases (e.g., custom submarket breakdowns).
- Run `make report` with the new config and confirm validation passes.

## Data Responsibilities
- Paid datasets live outside Git (`data/paid/`) and are referenced only through metadata and aggregates.
- Public datasets may be stored in `data/public/` with attribution and as-of dates.
- Reports must cite every factual metric, include retrieval dates, and pass leakage checks defined in `.github/workflows/validate_report.yml`.

## Compliance & Citations
- Follow the guardrails in `DATA_USAGE_POLICY.md`, `RISK_COMPLIANCE.md`, and `CITATION_STANDARDS.md`.
- CODEOWNERS enforces review from data stewards for changes touching critical directories.
- Scheduled workflows in `.github/workflows/` refresh data and validate outputs to maintain continuous compliance.

For deeper guidance, see `CONFIG.md` for configuration precedence and `CONTRIBUTING.md` for the development workflow.

## What this public release contains (honest scope)

This is a **curated public release** of the market-study tooling. The
product code (ETL pipelines, comp-scraping engines, rent-roll
standardization, forecasting, retrieval, report templates, prompts, tests)
ships in full. Data does **not**:

- **No property report trees.** The private workflow's per-property working
  data (`reports/<metro>/<property>/...`) — raw rent rolls, comp tracking
  histories, forecast backtests, generated report documents — is excluded
  wholesale. It contains live property, owner, and resident-level data
  (real rent rolls carry resident identifiers) and licensed comp snapshots.
  Public repos ship only the code and templates that generate such
  artifacts; the artifacts themselves are private outputs.
- **No research working data.** The `dislocation_analysis/` DFW
  equilibrium-study tree (datasets, results, dashboards) and
  `docs/superpowers/` internal working plans/runbooks are excluded as
  private analysis with org identity.
- **No internal operator files.** `.claude/` skills and agent memory,
  `.factory/` repo metadata, `AGENTS.md`/`CLAUDE.md` internal operating
  docs, and the private `CODEOWNERS`/`uv.lock` machine state are excluded.
- **Validation is synthetic-fixture-only.** The test suite runs against
  small synthetic fixtures in `tests/`. Parser and ETL coverage is not
  claimed beyond those fixtures and formats. The suite has pre-existing
  failures in the source repository (environment-dependent tests that
  require live data or paid datasets); see ACCEPTANCE.md for the exact,
  honest count and failure-set parity statement.
- Some modules that operate on external data paths (e.g.
  **your** local layout; path placeholders in this tree are generic, not
  live credentials or working mounts.

## Verification

```bash
make setup   # or: python3 -m venv .venv && pip install -r requirements.txt
make test    # pytest under tests/
```

See `ACCEPTANCE.md` for the release audit: suite counts, failure-set
parity against the source tree, and the privacy sweep.