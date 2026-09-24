# Risk & Compliance Playbook

This document defines how we protect licensed content, client confidentiality, and privacy while operating the Market Study Agent.

## Key Principles
- **License adherence**: Paid data stays off Git and out of public artifacts. Respect embargo, redistribution, and attribution clauses.
- **Client confidentiality**: Remove identifying client details prior to sharing reports externally.
- **PII exclusion**: ETL and retrieval stages must ignore or redact personal identifiers. Run leakage checks before publishing.

## Controls by Stage
### Ingestion & ETL
- Store public data in `data/public/` with readmes documenting provenance.
- Gate paid ingests (`data/paid/`) behind credentials. Keep audit logs of who accessed what by updating lineage manifests under `data/registry/lineage/`.
- Validators in `etl/utils/validators.py` should block rows missing license metadata or as-of dates.

### Retrieval & Prompting
- `retrieval/policies.yaml` configures what the agent can surface. Default: no verbatim or paraphrased paid text.
- Vector stores that might contain licensed snippets stay in local storage (`retrieval/stores/`) and are gitignored.

### Report Generation
- Automated guardrails check metrics schema compliance, citation completeness, and leakage keywords.
- Manual review required for any narrative referencing embargoed datasets or unpublished forecasts.

## Incident Response
1. **Identify**: File an issue tagged `compliance` and notify the data steward.
2. **Contain**: Remove offending artifacts from Git history, object storage, and any shared locations.
3. **Remediate**: Update guardrails (ETL validation, prompts, CI checks) to prevent recurrence.
4. **Review**: Document the incident and lessons learned in the compliance log.

## Approvals & Audit
- CODEOWNERS requires review from the data steward team for any change touching `data/`, `prompts/`, or `agents/`.
- Quarterly audits confirm licenses, lineage manifests, and retention schedules are in force.
