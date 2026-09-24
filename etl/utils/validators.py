"""Validation helpers to enforce canonical schema rules."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

REQUIRED_FIELDS = ["metric", "value", "as_of", "geo_level", "msa_code", "source_id"]


@dataclass
class ValidationIssue:
    level: str
    message: str
    record: dict[str, Any] | None = None


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues

    def add(self, level: str, message: str, record: dict[str, Any] | None = None) -> None:
        self.issues.append(ValidationIssue(level=level, message=message, record=record))


def validate_record(record: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for field_name in REQUIRED_FIELDS:
        if field_name not in record or record[field_name] in (None, ""):
            issues.append(
                ValidationIssue(
                    level="error",
                    message=f"Missing field: {field_name}",
                    record=record,
                )
            )
    as_of = record.get("as_of")
    if as_of:
        try:
            dt.datetime.fromisoformat(as_of[:10])
        except ValueError:
            issues.append(
                ValidationIssue(
                    level="error",
                    message="Invalid as_of format",
                    record=record,
                )
            )
    return issues


def build_report(records: list[dict[str, Any]]) -> ValidationReport:
    report = ValidationReport()
    for record in records:
        for issue in validate_record(record):
            report.add(issue.level, issue.message, issue.record)
    return report


def write_report(report: ValidationReport, path: str) -> None:
    issues_payload: list[dict[str, Any]] = [
        {"level": issue.level, "message": issue.message, "record": issue.record}
        for issue in report.issues
    ]
    summary = {
        "status": "passed" if not issues_payload else "failed",
        "issues": issues_payload,
    }
    import json
    from pathlib import Path

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
