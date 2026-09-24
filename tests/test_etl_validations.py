from __future__ import annotations

from etl.utils.validators import build_report


def test_build_report_flags_missing_fields() -> None:
    records = [
        {
            "metric": "vacancy_rate",
            "value": 7.5,
            "as_of": "2025-08-01",
            "geo_level": "msa",
            "msa_code": "12420",
            "source_id": "COSTAR-MKT",
        },
        {
            "metric": "rent_growth",
            "value": 2.4,
            "geo_level": "msa",
            "msa_code": "12420",
            "source_id": "COSTAR-MKT",
        },
    ]

    report = build_report(records)

    assert not report.passed
    assert any(issue.message == "Missing field: as_of" for issue in report.issues)


def test_build_report_success() -> None:
    records = [
        {
            "metric": "vacancy_rate",
            "value": 7.5,
            "as_of": "2025-08-01",
            "geo_level": "msa",
            "msa_code": "12420",
            "source_id": "COSTAR-MKT",
        }
    ]
    report = build_report(records)
    assert report.passed
