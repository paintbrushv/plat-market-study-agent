"""Weekly orchestrator entry point for the Gainesville listings tracker.

Source modules conform to:
    collect(catchment: Catchment, run_id: str) -> CollectionResult

This skeleton wires up locking, run-id generation, schema migration, and
run_log persistence. Dedup, parquet snapshots, and JSONL ops logging are
added in later tasks.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

import duckdb
import yaml

from etl.gainesville.dataclasses import (
    Catchment,
    CollectionResult,
    CollectionStatus,
    RawObservation,
)
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.locking import LockBusy, RunLock
from etl.gainesville.run_id import next_run_id
from etl.gainesville.snapshots import write_snapshot
from etl.gainesville.sources import apartments_com as ap_mod
from etl.gainesville.sources import cooke_cad_import as cad_mod
from etl.gainesville.sources import craigslist as craigslist_mod
from etl.gainesville.sources import property_direct as pd_mod
from etl.gainesville.sources import zillow as zillow_mod
from etl.gainesville.sources import zori as zori_mod
from etl.gainesville.sources.property_direct_config import PropertyDirectSite, parse_sites
from etl.gainesville.validate import is_for_sale_disguised, validate_and_clean

logger = logging.getLogger(__name__)


def load_catchment_from_yaml(config_path: Path) -> Catchment:
    """Load the ``catchment`` block from a YAML config and return a :class:`Catchment`.

    Raises:
        FileNotFoundError: if *config_path* does not exist.
        ValueError: if the ``catchment`` block is missing or lacks required keys.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    block = data.get("catchment")
    if not block or not isinstance(block, dict):
        raise ValueError(f"config {config_path} missing 'catchment' block")
    required = {"county_fips", "zip_codes", "bbox"}
    missing = required - set(block.keys())
    if missing:
        raise ValueError(f"catchment block missing keys: {sorted(missing)}")
    return Catchment(
        county_fips=str(block["county_fips"]),
        zip_codes=tuple(str(z) for z in block["zip_codes"]),
        bbox=tuple(float(v) for v in block["bbox"]),  # type: ignore[arg-type]
        city_limits_geojson_path=block.get("city_limits_geojson"),
    )


class Source(Protocol):
    name: str

    def collect(self, catchment: Catchment, run_id: str) -> CollectionResult: ...


@dataclass
class RunSummary:
    run_id: str
    status: str  # 'success' | 'partial' | 'failed' | 'lock_busy'
    sources_ok: list[str]
    sources_failed: list[str]
    new_observations: int


def run_weekly(
    *,
    db_path: Path,
    lock_path: Path,
    runs_dir: Path,
    catchment: Catchment,
    sources: list[Source],
    config: dict | None = None,
) -> RunSummary:
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        with RunLock(lock_path):
            return _run_inner(db_path, runs_dir, catchment, sources, config=config)
    except LockBusy as e:
        logger.error("lock busy: %s", e)
        return RunSummary("", "lock_busy", [], [], 0)


def _run_inner(
    db_path: Path,
    runs_dir: Path,
    catchment: Catchment,
    sources: list[Source],
    config: dict | None = None,
) -> RunSummary:
    conn = connect(db_path)
    apply_schema(conn)
    run_id = next_run_id(conn)
    started_at = dt.datetime.now(dt.UTC)
    sources_ok: list[str] = []
    sources_failed: list[str] = []
    new_obs = 0
    ops_log_path = runs_dir / f"{run_id}.jsonl"
    with ops_log_path.open("w", encoding="utf-8") as ops:
        payload = {"event": "run_start", "run_id": run_id, "ts": started_at.isoformat()}
        ops.write(json.dumps(payload) + "\n")
        for src in sources:
            t0 = dt.datetime.now(dt.UTC)
            try:
                result = src.collect(catchment, run_id)
            except Exception:
                tb = traceback.format_exc()
                err_payload = {"event": "source_error", "source": src.name, "traceback": tb}
                ops.write(json.dumps(err_payload) + "\n")
                sources_failed.append(src.name)
                continue
            if result.status != CollectionStatus.OK:
                sources_failed.append(src.name)
            else:
                sources_ok.append(src.name)
            ops.write(json.dumps({
                "event": "source_done",
                "source": src.name,
                "status": result.status.value,
                "observations": len(result.observations),
                "elapsed_s": (dt.datetime.now(dt.UTC) - t0).total_seconds(),
                "diagnostics": result.diagnostics,
            }) + "\n")
            new_obs += len(result.observations)
        finished_at = dt.datetime.now(dt.UTC)
        status = "success" if not sources_failed else ("partial" if sources_ok else "failed")
        from etl.gainesville import dedup
        dedup_summary = dedup.run_dedup(conn, run_id=run_id)
        dedup.mark_stale(conn)
        n_linked = dedup.attach_property_links(conn)
        ops_pending_dir = Path(
            (config or {}).get("paths", {}).get(
                "ops_dedup_pending", "reports/gainesville-tx/_ops"
            )
        )
        ops_pending_dir.mkdir(parents=True, exist_ok=True)
        review_csv = ops_pending_dir / f"dedup_review_{run_id}.csv"
        n_review = dedup.export_review_queue(conn, run_id=run_id, out_path=review_csv)
        ops.write(json.dumps({
            "event": "dedup_done",
            "new_canonicals": dedup_summary.new_canonicals,
            "merged": dedup_summary.merged,
            "review_queued": n_review,
            "property_links": n_linked,
        }) + "\n")
        conn.execute(
            "INSERT INTO run_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, started_at, finished_at, status, sources_ok, sources_failed,
             new_obs, dedup_summary.new_canonicals, n_review, ""],
        )
        ops.write(
            json.dumps({"event": "run_end", "status": status, "ts": finished_at.isoformat()}) + "\n"
        )
    return RunSummary(run_id, status, sources_ok, sources_failed, new_obs)


@dataclass
class _ZoriSource:
    name: str
    db_path: Path
    csv_path_override: Path | None = None
    zips: tuple[str, ...] | None = None  # zip-list override; when set, narrows the catchment scope

    def collect(self, catchment: Catchment, run_id: str) -> CollectionResult:
        from dataclasses import replace as _replace

        scope = (
            _replace(catchment, zip_codes=tuple(self.zips))
            if self.zips
            else catchment
        )
        conn = connect(self.db_path)
        if self.csv_path_override is not None:
            return zori_mod.collect_from_csv(self.csv_path_override, scope, conn, "all")
        return zori_mod.collect_from_url(scope, conn, "all")


@dataclass
class _CookeCADSource:
    name: str
    db_path: Path
    klement_db_path: Path
    property_types: list[str]
    owner_name_keywords: list[str]
    required_columns: list[str] | None = None
    geocode_cache_path: Path | None = None
    parcel_id_exclusions: list[str] = field(default_factory=list)

    def collect(self, catchment: Catchment, run_id: str) -> CollectionResult:
        conn = connect(self.db_path)
        poly_path = (
            Path(catchment.city_limits_geojson_path)
            if catchment.city_limits_geojson_path
            else None
        )
        return cad_mod.import_parcels(
            klement_db_path=self.klement_db_path,
            target_conn=conn,
            county_fips=catchment.county_fips,
            property_types=self.property_types,
            owner_name_keywords=self.owner_name_keywords,
            required_columns=self.required_columns,
            city_limits_polygon_path=poly_path,
            geocode_cache_path=self.geocode_cache_path,
            parcel_id_exclusions=self.parcel_id_exclusions if self.parcel_id_exclusions else None,
        )


def _insert_observation(conn: duckdb.DuckDBPyConnection, obs: RawObservation) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO raw_observations VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE)",
        [
            obs.observation_id, obs.run_id, obs.source, obs.source_listing_id, obs.url,
            obs.scraped_at, obs.listing_kind, obs.address_raw, obs.address_normalized,
            obs.addr_norm_version, obs.city, obs.zip, obs.lat, obs.lon, obs.beds, obs.baths,
            obs.sqft, obs.asking_rent, obs.concessions_text, obs.date_posted,
            obs.date_available, obs.title, obs.body, obs.raw_payload_path,
        ],
    )


def _drop_for_sale_disguised(
    observations: list[RawObservation],
) -> tuple[list[RawObservation], list[tuple[str, str]]]:
    """Filter out for-sale listings disguised as rentals.

    Returns a ``(kept, dropped_with_reason)`` pair where ``dropped_with_reason``
    is a list of ``(source_listing_id, reason)`` tuples for diagnostics.
    """
    kept: list[RawObservation] = []
    dropped: list[tuple[str, str]] = []
    for obs in observations:
        reason = is_for_sale_disguised(obs)
        if reason is not None:
            dropped.append((obs.source_listing_id, reason))
            logger.info(
                "dropped for-sale listing disguised as rental: %s (%s)", obs.url, reason
            )
        else:
            kept.append(obs)
    return kept, dropped


@dataclass
class _CraigslistSource:
    name: str
    db_path: Path
    parquet_history_mf: Path
    parquet_history_sfr: Path
    regions: list[str]
    keywords: list[str]
    fetch_fn: Callable[..., str] = craigslist_mod.fetch  # type: ignore[assignment]
    delay_s: float = craigslist_mod.REQUEST_DELAY_S

    def collect(self, catchment: Catchment, run_id: str) -> CollectionResult:
        result = craigslist_mod.collect(
            catchment, run_id,
            regions=self.regions,
            keywords=self.keywords,
            fetch_fn=self.fetch_fn,
            delay_s=self.delay_s,
        )
        validated: list[RawObservation] = []
        all_issues: list[str] = []
        for obs in result.observations:
            o, issues = validate_and_clean(obs)
            validated.append(o)
            all_issues.extend(issues)
        cleaned, dropped_for_sale = _drop_for_sale_disguised(validated)
        result.diagnostics["dropped_for_sale"] = len(dropped_for_sale)
        snapshot_paths = write_snapshot(
            observations=cleaned,
            run_id=run_id,
            source=self.name,
            base_dir_mf=self.parquet_history_mf,
            base_dir_sfr=self.parquet_history_sfr,
        )
        # Build a lookup: "sfr" kind → sfr snapshot path; anything else → mf path.
        sfr_path = next((p for p in snapshot_paths if "sfr" in str(p)), None)
        mf_path = next(
            (p for p in snapshot_paths if "sfr" not in str(p)), None
        )
        conn = connect(self.db_path)
        apply_schema(conn)
        for o in cleaned:
            expected_path = sfr_path if o.listing_kind == "sfr" else mf_path
            o_with_path = (
                o if expected_path is None else replace(o, raw_payload_path=str(expected_path))
            )
            _insert_observation(conn, o_with_path)
        result.diagnostics["validation_issues"] = all_issues
        return result


@dataclass
class _ApartmentsComSource:
    name: str
    db_path: Path
    parquet_history_mf: Path
    parquet_history_sfr: Path
    test_htmls: list[str] | None = None

    def collect(self, catchment: Catchment, run_id: str) -> CollectionResult:
        if self.test_htmls is not None:
            result = ap_mod.collect_from_html(self.test_htmls, run_id)
        else:
            result = ap_mod.collect(catchment, run_id)
        validated: list[RawObservation] = []
        for obs in result.observations:
            o, _ = validate_and_clean(obs)
            validated.append(o)
        cleaned, dropped_for_sale = _drop_for_sale_disguised(validated)
        result.diagnostics["dropped_for_sale"] = len(dropped_for_sale)
        snapshot_paths = write_snapshot(
            observations=cleaned,
            run_id=run_id,
            source=self.name,
            base_dir_mf=self.parquet_history_mf,
            base_dir_sfr=self.parquet_history_sfr,
        )
        # All apartments.com listings are 'mf'; grab the first (and only) mf path.
        mf_path = next((p for p in snapshot_paths if "sfr" not in str(p)), None)
        conn = connect(self.db_path)
        apply_schema(conn)
        for o in cleaned:
            o2 = o if mf_path is None else replace(o, raw_payload_path=str(mf_path))
            _insert_observation(conn, o2)
        return result


@dataclass
class _PropertyDirectSource:
    name: str
    db_path: Path
    parquet_history_mf: Path
    parquet_history_sfr: Path
    sites: list[PropertyDirectSite]
    test_html_overrides: dict[str, str] | None = None

    def collect(self, catchment: Catchment, run_id: str) -> CollectionResult:
        all_obs: list[RawObservation] = []
        statuses: list[str] = []
        for site in self.sites:
            html = (self.test_html_overrides or {}).get(site.url)
            if html is None:
                result = pd_mod.collect(catchment, run_id, sites=[site])
            else:
                result = pd_mod.collect_from_html(site, html, run_id)
            all_obs.extend(result.observations)
            statuses.append(f"{site.name}:{result.status.value}")
        validated: list[RawObservation] = []
        for obs in all_obs:
            o, _ = validate_and_clean(obs)
            validated.append(o)
        cleaned, dropped_for_sale = _drop_for_sale_disguised(validated)
        snapshot_paths = write_snapshot(
            observations=cleaned,
            run_id=run_id,
            source=self.name,
            base_dir_mf=self.parquet_history_mf,
            base_dir_sfr=self.parquet_history_sfr,
        )
        # property_direct rows are always 'mf'; grab the mf snapshot path.
        mf_path = next((p for p in snapshot_paths if "sfr" not in str(p)), None)
        conn = connect(self.db_path)
        apply_schema(conn)
        for o in cleaned:
            o2 = o if mf_path is None else replace(o, raw_payload_path=str(mf_path))
            _insert_observation(conn, o2)
        if not cleaned:
            return CollectionResult([], CollectionStatus.ZERO_RESULTS_SUSPICIOUS,
                                    {"sites": statuses, "dropped_for_sale": len(dropped_for_sale)})
        return CollectionResult(cleaned, CollectionStatus.OK,
                                {"sites": statuses, "dropped_for_sale": len(dropped_for_sale)})


@dataclass
class _ZillowSource:
    name: str
    db_path: Path
    parquet_history_mf: Path
    parquet_history_sfr: Path
    cookie_state_path: Path | None = None
    _test_htmls: list[str] | None = None

    def collect(self, catchment: Catchment, run_id: str) -> CollectionResult:
        if self._test_htmls is not None:
            result = zillow_mod.collect_from_html(self._test_htmls, run_id)
        else:
            result = zillow_mod.collect(
                catchment, run_id, cookie_state_path=self.cookie_state_path,
            )
        validated: list[RawObservation] = []
        for obs in result.observations:
            o, _ = validate_and_clean(obs)
            validated.append(o)
        cleaned, dropped_for_sale = _drop_for_sale_disguised(validated)
        result.diagnostics["dropped_for_sale"] = len(dropped_for_sale)
        snapshot_paths = write_snapshot(
            observations=cleaned,
            run_id=run_id,
            source=self.name,
            base_dir_mf=self.parquet_history_mf,
            base_dir_sfr=self.parquet_history_sfr,
        )
        sfr_path = next((p for p in snapshot_paths if "sfr" in str(p)), None)
        mf_path = next((p for p in snapshot_paths if "sfr" not in str(p)), None)
        conn = connect(self.db_path)
        apply_schema(conn)
        for o in cleaned:
            expected_path = sfr_path if o.listing_kind == "sfr" else mf_path
            o2 = o if expected_path is None else replace(o, raw_payload_path=str(expected_path))
            _insert_observation(conn, o2)
        return result


def build_sources_from_config(
    config: dict,
    db_path: Path,
    *,
    cadence: str = "weekly",
) -> list[Source]:
    """Build a list of :class:`Source` instances from a parsed YAML config dict.

    Args:
        config: Parsed YAML config dict.
        db_path: Path to the DuckDB database.
        cadence: One of ``"weekly"``, ``"monthly"``, ``"quarterly"`` (or any key
            whose ``{cadence}_sources`` list exists in the ``cadence`` block).
            When the config contains a non-empty ``cadence`` block, only sources
            listed under ``{cadence}_sources`` are included.  If the ``cadence``
            block is absent or empty, all enabled sources are included (preserves
            behaviour for configs and tests that do not have a cadence block).
    """
    cadence_block = config.get("cadence") or {}
    cadence_key = f"{cadence}_sources"
    allowed: set[str] | None = (
        set(cadence_block[cadence_key]) if cadence_block and cadence_key in cadence_block else None
    )
    src_cfg = config.get("sources", {})
    sources: list[Source] = []
    if src_cfg.get("zori", {}).get("enabled") and (allowed is None or "zori" in allowed):
        override = src_cfg["zori"].get("csv_path_override")
        zips = src_cfg["zori"].get("zips")
        sources.append(_ZoriSource(
            name="zori",
            db_path=db_path,
            csv_path_override=Path(override) if override else None,
            zips=tuple(zips) if zips else None,
        ))
    if src_cfg.get("cooke_cad_import", {}).get("enabled") and (
        allowed is None or "cooke_cad_import" in allowed
    ):
        cad = src_cfg["cooke_cad_import"]
        asset_filter = cad.get("asset_class_filter", {})
        paths_block = config.get("paths", {})
        geocode_cache_raw = paths_block.get("geocode_cache", "data/local/.geocode_cache.json")
        excl_raw = asset_filter.get("parcel_id_exclusions") or []
        excl_ids = [str(entry["parcel_id"]) for entry in excl_raw if isinstance(entry, dict)]
        sources.append(_CookeCADSource(
            name="cooke_cad_import",
            db_path=db_path,
            klement_db_path=Path(cad["klement_db_path"]),
            property_types=asset_filter.get("property_types", ["residential_mf"]),
            owner_name_keywords=asset_filter.get("owner_name_keywords", []),
            required_columns=cad.get("required_columns"),
            geocode_cache_path=Path(geocode_cache_raw),
            parcel_id_exclusions=excl_ids,
        ))
    cl_cfg = src_cfg.get("craigslist", {})
    if cl_cfg.get("enabled") and (allowed is None or "craigslist" in allowed):
        paths = config.get("paths", {})
        sources.append(_CraigslistSource(
            name="craigslist",
            db_path=db_path,
            parquet_history_mf=Path(paths.get(
                "parquet_history_mf",
                "reports/gainesville-tx/multifamily/_universe/listings_history",
            )),
            parquet_history_sfr=Path(paths.get(
                "parquet_history_sfr",
                "reports/gainesville-tx/sfr/listings_history",
            )),
            regions=cl_cfg.get("regions", ["dallas"]),
            keywords=cl_cfg.get("keywords", ["gainesville"]),
            fetch_fn=cl_cfg.get("_test_fetch_fn") or craigslist_mod.fetch,
            delay_s=cl_cfg.get("_test_delay_s", craigslist_mod.REQUEST_DELAY_S),
        ))
    ap_cfg = src_cfg.get("apartments_com", {})
    if ap_cfg.get("enabled") and (allowed is None or "apartments_com" in allowed):
        paths = config.get("paths", {})
        sources.append(_ApartmentsComSource(
            name="apartments_com",
            db_path=db_path,
            parquet_history_mf=Path(paths.get(
                "parquet_history_mf",
                "reports/gainesville-tx/multifamily/_universe/listings_history",
            )),
            parquet_history_sfr=Path(paths.get(
                "parquet_history_sfr",
                "reports/gainesville-tx/sfr/listings_history",
            )),
            test_htmls=ap_cfg.get("_test_htmls"),
        ))
    pd_cfg = src_cfg.get("property_direct", {})
    if pd_cfg.get("enabled") and (allowed is None or "property_direct" in allowed):
        sites = parse_sites(pd_cfg.get("sites", []))
        if sites:
            paths = config.get("paths", {})
            sources.append(_PropertyDirectSource(
                name="property_direct",
                db_path=db_path,
                parquet_history_mf=Path(paths.get(
                    "parquet_history_mf",
                    "reports/gainesville-tx/multifamily/_universe/listings_history",
                )),
                parquet_history_sfr=Path(paths.get(
                    "parquet_history_sfr",
                    "reports/gainesville-tx/sfr/listings_history",
                )),
                sites=sites,
                test_html_overrides=pd_cfg.get("_test_html_overrides"),
            ))
    z_cfg = src_cfg.get("zillow", {})
    if z_cfg.get("enabled") and (allowed is None or "zillow" in allowed):
        paths = config.get("paths", {})
        cookie_path = z_cfg.get("cookie_state_path")
        sources.append(_ZillowSource(
            name="zillow",
            db_path=db_path,
            parquet_history_mf=Path(paths.get(
                "parquet_history_mf",
                "reports/gainesville-tx/multifamily/_universe/listings_history",
            )),
            parquet_history_sfr=Path(paths.get(
                "parquet_history_sfr",
                "reports/gainesville-tx/sfr/listings_history",
            )),
            cookie_state_path=Path(cookie_path) if cookie_path else None,
            _test_htmls=z_cfg.get("_test_htmls"),
        ))
    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gainesville listings weekly run.")
    parser.add_argument("--db", type=Path, default=Path("data/local/gainesville.duckdb"))
    parser.add_argument("--lock", type=Path, default=Path("data/local/.gainesville.lock"))
    parser.add_argument("--runs-dir", type=Path, default=Path("reports/gainesville-tx/_ops/runs"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("agents/configs/gainesville_tx.yaml"),
        help="Path to the YAML config containing the 'catchment' block.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--cadence",
        default="weekly",
        choices=["weekly", "monthly", "quarterly"],
        help="Which cadence bucket of sources to run (default: weekly).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.dry_run:
        print("dry-run: would acquire lock and run zero sources")
        return 0
    try:
        catchment = load_catchment_from_yaml(args.config)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("config error: %s", exc)
        return 2
    config_dict = (
        yaml.safe_load(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    )
    sources = build_sources_from_config(config_dict, db_path=args.db, cadence=args.cadence)
    summary = run_weekly(
        db_path=args.db,
        lock_path=args.lock,
        runs_dir=args.runs_dir,
        catchment=catchment,
        sources=sources,
    )
    print(f"run_id={summary.run_id} status={summary.status}")
    return 0 if summary.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
