# ACCEPTANCE — plat-market-study-agent public release audit

Date: 2026-09-24. Staged tree: this repository (the public release staging copy)
built from the private source repo HEAD
(branch `next-stage/20260918T024342Z`, commit `fbf87cb`).

This file records what was verified for the staged public release tree. The
tree is a curated export of the source working tree (tracked files present on
disk) — **private branch history is not part of this release** (fresh-public-
repo import strategy; see import commit message at publish time).

## 1. Curation decisions (excluded from the public tree)

| Excluded | Reason |
|---|---|
| `reports/` per-property trees (raw rent rolls, comp tracking, forecast backtests, generated report .md/.html, `reports/gainesville-tx/_ops` run logs) | Live property/owner/resident data (real rent rolls carry resident names and lease dates); licensed comp snapshots; generated deal outputs. Only the `reports/published/` + `reports/logs/` skeleton ships (gitkeep). |
| `dislocation_analysis/` (DFW equilibrium research: datasets, results, dashboards) | Private working analysis with org identity; not product code. |
| `data/` (public datasets, IRS SOI, LODES, processed comp snapshots, LLM extraction cache, registry) | Deal-specific processed data and scraped site cache; public raw datasets are re-downloadable via `etl/` ingest scripts and `data/registry/` metadata stays private by scope decision. |
| `docs/superpowers/` (working plans, specs, runbooks) | Internal operator docs with personal emails, host paths, org identity. |
| `.claude/` (skills, error memory, agent settings) | Internal operator files; `settings.local.json` carries a personal workstation git path. |
| `.factory/`, `AGENTS.md`, `CLAUDE.md`, `CODEOWNERS`, `uv.lock` | Internal repo metadata / machine-state lockfile; CODEOWNERS placeholder until the public org exists. |
| `SOURCE_REGISTRY.csv` | Sparse-hidden dataset registry referencing private data paths. |

## 2. Sanitization (identity scrub)

Scrubbed in the staged tree (all occurrences, verified zero remaining):

- Personal email (the maintainer's personal email address) → generic maintainer
  placeholder — 5 hits (User-Agent strings in gainesville ETL sources,
  geocode, tests/refresh_fixtures).
- Personal macOS paths (personal `~/projects/...` absolute paths) → generic
  `~/projects/...` / `<repo>`-relative placeholders — 13 hits (cron scripts,
  metro configs, sync module, test).
- Personal-handle User-Agent string and personal-domain docstring → generic.
- Company identity in a property_id literal → neutral property name;
  (company-named base-path constant → `OPS_BASE`, docstring de-companied; test import
  updated).
- `etl/gainesville/cron/*.sh`: hard-coded `/opt/homebrew/bin/uv` and absolute
  repo path → portable `UV` env default and script-relative `REPO`.
- Kept-by-design: ordinary-English domain words were distinguished from company
  tokens before scrubbing (kept where legitimate).

Post-sweep grep for company tokens, personal names/emails/handles, and personal host paths over the
staged tree: **zero identity hits** (verified with the final sweep, §5).

## 3. Environment-dependent tests — adaptations in the public tree

The source suite (sparse privacy overlay on the workstation) had 8 failing /
474 total tests at HEAD. Failure-set analysis:

- 5 × gainesville ZORI tests: failed ONLY because the sparse overlay excluded
  two synthetic fixture CSVs (`zori_sample.csv`, `zori_realistic.csv`) from
  disk. Both fixtures are synthetic (4-row ZORI-shaped sample data, no
  identity) and are **restored in the public tree from git HEAD** → pass.
- 1 × `test_pull_comps_tool_returns_rows_with_source_attribution`: ran against
  a live comps snapshot dir (private data, excluded). Public tree: monkeypatch
  synthetic snapshot loader → same contract asserted, passes.
- 1 × `test_crossings_mapping_skips_subheaders_and_future_applicants`: read a
  live deal-room rent roll from a sibling private repo (also carried a
  personal macOS path). Public tree: skip-with-reason when the (unshipped)
  input is absent → honest skip.
- 1 × `test_load_panel_uses_ctx_parquet`: read a paid CoStar parquet
  (`data/paid/`, never shipped). Public tree: synthetic parquet of the same
  schema exercises the same ctx-parquet contract → passes.

**Zero pre-existing source failures were silently dropped; each is either
restored (fixture) or re-based on synthetic inputs with the dependency stated
in the test docstring/skip reason.**

## 4. Suite verification (the release gate)

All runs under `umask 077`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, explicit
basetemp outside `/tmp`:

| Run | Tree | Result |
|---|---|---|
| baseline | source repo, overlay state | 474 tests: 8 failed / 27 skipped |
| public | staged public tree | **474 tests: 0 failed / 28 skipped, exit 0** |

Test-ID parity vs baseline: 474/474 collected, 0 missing, 0 extra — one test
module renamed with its sanitized source module (same test name, both pass).
Skip delta (+1) is the explicitly-reasoned live-data skip (§3).

## 5. Privacy sweep (final, on the staged tree)

Tracked-file grep equivalents over the staged tree for: personal email,
personal name, personal macOS paths, private-host paths, hostname, company tokens,
`/Users/` — zero identity hits. The `.env.example` ships empty placeholders
only. No secrets, tokens, or credentials in the tree (verified by pattern
scan).

## 6. Honest limitations

- **Synthetic-only validation.** All shipped fixtures are synthetic; parser /
  ETL / forecast coverage is claimed only for those fixtures and formats.
- Tests requiring live data (property report trees, paid CoStar parquets, the
  deal-room rent roll) skip with explicit reasons — they are NOT deleted, and
  their absence is stated in each skip message.
  gainesville cron entrypoints, metro configs with local DB paths) expect the
  operator's own local layout; placeholders are generic, not working mounts.
- Model metrics: none — no model runs are part of this release's verification.