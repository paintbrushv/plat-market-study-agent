"""Per-source parquet snapshots — the audit trail.

Written before any DB writes for the source. If DuckDB barfs mid-run,
--replay-from-parquet rebuilds rows from these files.

Mixed-kind batches are split: SFR observations go to *base_dir_sfr*; all
other kinds (mf, duplex, fourplex, unknown) go to *base_dir_mf*.
``write_snapshot`` returns a list of paths — one per non-empty partition.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from etl.gainesville.dataclasses import RawObservation


def write_snapshot(
    *,
    observations: list[RawObservation],
    run_id: str,
    source: str,
    base_dir_mf: Path,
    base_dir_sfr: Path,
) -> list[Path]:
    """Write parquet snapshot(s) for *observations* partitioned by listing_kind.

    Returns a (possibly empty) list of paths written:
    - SFR observations → *base_dir_sfr*/<run_id>/<source>.parquet
    - Non-SFR observations → *base_dir_mf*/<run_id>/<source>.parquet
    """
    if not observations:
        return []
    sfr_obs = [o for o in observations if o.listing_kind == "sfr"]
    mf_obs = [o for o in observations if o.listing_kind != "sfr"]
    paths: list[Path] = []
    if sfr_obs:
        paths.append(_write_partition(sfr_obs, run_id, source, base_dir_sfr))
    if mf_obs:
        paths.append(_write_partition(mf_obs, run_id, source, base_dir_mf))
    return paths


def _write_partition(
    observations: list[RawObservation],
    run_id: str,
    source: str,
    base_dir: Path,
) -> Path:
    out_dir = base_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{source}.parquet"
    rows = [asdict(o) for o in observations]
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, out_path)
    return out_path
