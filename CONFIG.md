# Configuration Guide

This repository keeps configuration declarative so that new metros, data sources, and workflows can be added without touching code. The hierarchy below shows how values are resolved at runtime.

## Precedence Order
1. **CLI flags** passed to runners (for example, `--report-date 2025-10-29`).
2. **Environment variables** in `.env` or the host environment.
3. **Agent config files** under `agents/configs/` (metro-specific YAML).
4. **Application defaults** embedded in runner scripts.

Higher layers always override lower ones, allowing you to customize runs on the fly while keeping sane defaults in version control.

## Metro Configs
Each metro has a YAML file following `agents/configs/template.yaml`. Key sections:
- `metro`, `asset_class`, and `purpose` describe the engagement.
- `primary_sources` map report needs to dataset IDs defined in `data/registry/datasets.yaml`.
- `retrieval` toggles guardrails for public vs. paid content.
- `citing` enforces style and attribution requirements.
- `outputs` controls report destinations. Placeholders such as `{date}` and `{metro_slug}` are resolved by the runner.
- `validation` lists required tables and hard-stop errors.

## Environment Variables
Use `.env.example` as a reference and load real values via `.env` or GitHub Actions secrets. Credentials are never committed; the agent expects them at runtime.

## Switch Primary Sources
To change providers (e.g., swap CoStar for Yardi), update the `primary_sources` section of a config file to reference the correct dataset ID. No code changes are required as long as ETL scripts understand that dataset.

## Output Conventions
Runners expand the configured path and ensure directories exist before writing. Reports should live in `reports/published/` with the naming convention `{YYYY}-{MM}-{DD}_{Metro}_MF.md`. Provenance logs belong under `reports/logs/` with ISO timestamps.
