from __future__ import annotations

from pathlib import Path

import pytest
from etl.gainesville.db import apply_schema, connect


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "test.duckdb"


def test_apply_schema_creates_all_tables(fresh_db: Path) -> None:
    conn = connect(fresh_db)
    apply_schema(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).fetchall()}
    assert tables == {
        "property_register",
        "raw_observations",
        "canonical_listings",
        "run_log",
        "dedup_review",
        "zori_history",
        "_schema_migrations",
    }


def test_apply_schema_is_idempotent(fresh_db: Path) -> None:
    conn = connect(fresh_db)
    apply_schema(conn)
    apply_schema(conn)  # second call must not error
    count = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='main'"
    ).fetchone()[0]
    assert count == 7


def test_raw_observations_required_columns(fresh_db: Path) -> None:
    conn = connect(fresh_db)
    apply_schema(conn)
    cols = {row[0] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='raw_observations'"
    ).fetchall()}
    expected = {
        "observation_id", "run_id", "source", "source_listing_id", "url",
        "scraped_at", "listing_kind", "address_raw", "address_normalized",
        "addr_norm_version", "city", "zip", "lat", "lon", "beds", "baths",
        "sqft", "asking_rent", "concessions_text", "date_posted",
        "date_available", "title", "body", "raw_payload_path",
    }
    assert expected.issubset(cols)
