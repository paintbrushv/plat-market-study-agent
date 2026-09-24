# Data Usage Policy

This project separates public and paid sources to respect licensing, privacy, and non-republication agreements.

## Paid / Subscriber Data
- **Paid or subscriber datasets** (CoStar, Yardi Matrix, RealPage, etc.) MUST NOT be committed to the repository or redistributed.
- Derived outputs that could be reverse-engineered to original paid data stay internal only. Publish aggregated metrics, not raw downloads.
- CoStar Market Analytics (COSTAR-MKT) usage is limited to aggregated metrics with `[COSTAR-MKT, as-of, Aggregated]` citations; never index or reproduce verbatim CoStar narrative.
- Access is limited to authorized teammates. Keep credentials in secret managers, never in version control.
- Follow vendor-specific embargo and attribution rules recorded in `data/registry/datasets.yaml`.

## Public / Open Data
- Public datasets (BLS, Census, HUD) may be stored under `data/public/` subject to the dataset license.
- Always retain source metadata and as-of dates so provenance can be traced.
- When mirroring large public datasets, evaluate whether Git LFS, DVC, or cloud object storage is more appropriate than Git history.

## Reporting & Publication
All exports to `reports/published/` must:
1. Contain aggregated metrics only—no raw rows from paid sources.
2. Include explicit source citations and as-of dates.
3. Pass the leakage checks implemented in `.github/workflows/validate_report.yml`.

Violations of this policy trigger incident response steps outlined in `RISK_COMPLIANCE.md`.
