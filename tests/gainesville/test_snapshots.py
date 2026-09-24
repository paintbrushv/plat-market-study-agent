from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow.parquet as pq
from etl.gainesville.dataclasses import RawObservation
from etl.gainesville.snapshots import write_snapshot


def _obs(i: int, kind: str = "sfr") -> RawObservation:
    return RawObservation(
        observation_id=f"obs-{i}",
        run_id="2026-W19_gainesville",
        source="craigslist",
        source_listing_id=f"cl-{i}",
        url=f"https://example.com/{i}",
        scraped_at=dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.UTC),
        listing_kind=kind,
        address_raw=f"{i} Main St",
        address_normalized=f"{i} main street",
        addr_norm_version=1,
        city="Gainesville",
        zip="76240",
        lat=33.6,
        lon=-97.1,
        beds=2.0,
        baths=1.0,
        sqft=900,
        asking_rent=1200,
        concessions_text=None,
        date_posted=dt.date(2026, 5, 10),
        date_available=None,
        title=f"Listing {i}",
        body="body",
        raw_payload_path=None,
    )


def test_write_snapshot_creates_parquet(tmp_path: Path) -> None:
    """All-SFR batch returns a single-element list with the sfr path."""
    obs = [_obs(i) for i in range(3)]
    paths = write_snapshot(
        observations=obs,
        run_id="2026-W19_gainesville",
        source="craigslist",
        base_dir_mf=tmp_path / "mf",
        base_dir_sfr=tmp_path / "sfr",
    )
    assert isinstance(paths, list)
    assert len(paths) == 1
    path = paths[0]
    assert path.exists()
    assert path.suffix == ".parquet"
    assert "sfr" in str(path)  # sfr-only batch goes to sfr base dir
    table = pq.read_table(path)
    assert table.num_rows == 3
    assert "observation_id" in table.column_names


def test_write_snapshot_splits_mixed_kinds(tmp_path: Path) -> None:
    """A batch with SFR + MF + duplex is split into two separate parquet files."""
    obs = [_obs(0, "sfr"), _obs(1, "mf"), _obs(2, "duplex")]
    paths = write_snapshot(
        observations=obs,
        run_id="2026-W19_gainesville",
        source="craigslist",
        base_dir_mf=tmp_path / "mf",
        base_dir_sfr=tmp_path / "sfr",
    )
    assert isinstance(paths, list)
    assert len(paths) == 2

    sfr_paths = [p for p in paths if "sfr" in str(p)]
    mf_paths = [p for p in paths if "sfr" not in str(p)]
    assert len(sfr_paths) == 1
    assert len(mf_paths) == 1

    sfr_table = pq.read_table(sfr_paths[0])
    assert sfr_table.num_rows == 1  # only the 1 SFR observation

    mf_table = pq.read_table(mf_paths[0])
    assert mf_table.num_rows == 2  # MF + duplex observations


def test_write_snapshot_skips_when_empty(tmp_path: Path) -> None:
    """Empty observations list returns an empty list."""
    paths = write_snapshot(
        observations=[],
        run_id="2026-W19_gainesville",
        source="craigslist",
        base_dir_mf=tmp_path / "mf",
        base_dir_sfr=tmp_path / "sfr",
    )
    assert paths == []
