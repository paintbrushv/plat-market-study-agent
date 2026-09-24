"""Deterministically build and emit the comp-finder BridgeResponseV1.

This script is the final step of the market-study-agent comp-finder flow.
It reads the already-written federation artifacts from `<deal_root>/outputs/
<run_id>/...`, validates them against the shared plat-agent contracts, writes
`market_study/_response_envelope.json`, and prints that exact JSON to stdout.

The goal is to eliminate the Belle Mor failure mode where the sibling wrote
real artifacts to disk but returned empty stdout, forcing plat-agent to
salvage the response after the fact.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PLAT_AGENT_SRC = (REPO_ROOT.parent / "plat-agent" / "src").resolve()
if str(PLAT_AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(PLAT_AGENT_SRC))

from plat_agent.contracts.domain.market_study import CompFinderResponse
from plat_agent.contracts.domain.market_study import cohort_key
from plat_agent.contracts.envelope import (
    ArtifactRef,
    BridgeError,
    BridgeResponseV1,
    ProvenanceEntry,
)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        Path(tmp).replace(path)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        finally:
            raise


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _normalize_status(raw: Any) -> str:
    if raw in {"ok", "needs_analyst_input", "error"}:
        return str(raw)
    return "error"


def _slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
    return slug.strip("_")


def _normalize_market_slug(value: Any) -> str | None:
    slug = _slugify(str(value or ""))
    return slug or None


def _display_market_name(value: Any, *, metro_slug: str | None) -> str | None:
    text = str(value or "").strip()
    if text:
        return text
    if metro_slug:
        return metro_slug.replace("_", " ").title()
    return None


def _as_float(value: Any) -> float | None:
    if value in (None, "", "Unknown"):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    parsed = _as_float(value)
    return int(parsed) if parsed is not None else None


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _normalize_ownership_type(value: Any) -> str | None:
    s = str(value or "").strip().lower()
    if not s or s == "unknown":
        return None
    if "reit" in s:
        return "reit"
    if "equity" in s:
        return "private_equity"
    if "private" in s:
        return "private"
    return "other"


def _normalize_renovation_status(value: Any) -> str | None:
    s = str(value or "").strip().lower()
    if not s or s == "unknown":
        return None
    if "full" in s or "platinum" in s or "renovat" in s:
        return "renovated"
    if "partial" in s or "light" in s:
        return "partial"
    if "classic" in s or "original" in s:
        return "classic"
    return "unknown"


def _find_subject_context(deal_root: Path, run_id: str) -> dict[str, Any]:
    canonical_path = deal_root / "outputs" / run_id / "intake" / "canonical_deal.json"
    canonical = _load_json(canonical_path) or {}
    metadata = canonical.get("metadata") or {}
    market_slug = _normalize_market_slug(metadata.get("market"))
    return {
        "canonical": canonical,
        "subject_name": metadata.get("deal_id"),
        "subject_address": metadata.get("address"),
        "market_slug": market_slug,
        "market_display": _display_market_name(metadata.get("market"), metro_slug=market_slug),
    }


def _find_property_config(
    *,
    market_slug: str | None,
    subject_name: str | None,
    subject_address: str | None,
) -> Path | None:
    config_dir = REPO_ROOT / "agents" / "configs"
    if not config_dir.exists():
        return None
    normalized_name = _slugify(subject_name or "")
    normalized_address = (subject_address or "").strip().lower()
    pattern = f"{market_slug}_*.yaml" if market_slug else "*.yaml"
    for candidate in sorted(config_dir.glob(pattern)):
        try:
            cfg = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        subject = ((cfg.get("notes") or {}).get("subject_property") or {})
        cfg_name = _slugify(subject.get("name") or "")
        cfg_address = str(subject.get("address") or "").strip().lower()
        if normalized_name and cfg_name == normalized_name:
            return candidate
        if normalized_address and cfg_address == normalized_address:
            return candidate
    return None


def _find_property_reports_dir(config_path: Path | None) -> Path | None:
    if config_path is None:
        return None
    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    subjects = ((cfg.get("comp_monitoring") or {}).get("subjects") or [])
    for subject in subjects:
        fp = subject.get("floorplan_summary_csv")
        if isinstance(fp, str) and fp:
            return (REPO_ROOT / fp).resolve().parents[2]
    report_path = ((cfg.get("outputs") or {}).get("report_path"))
    if isinstance(report_path, str) and report_path:
        return (REPO_ROOT / report_path).resolve().parent
    return None


def _latest_by_property(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    latest: dict[str, list[dict[str, str]]] = {}
    latest_date: dict[str, str] = {}
    for row in rows:
        prop = str(row.get("property") or "").strip()
        date = str(row.get("date") or "").strip()
        if not prop or not date:
            continue
        if prop not in latest_date or date > latest_date[prop]:
            latest[prop] = [row]
            latest_date[prop] = date
        elif date == latest_date[prop]:
            latest[prop].append(row)
    return latest


def _build_unit_type_entry(
    rent_row: dict[str, str],
    concession_by_property: dict[str, dict[str, str]],
) -> dict[str, Any]:
    concession_row = concession_by_property.get(str(rent_row.get("property") or "").strip(), {})
    evidence_quality = _classify_evidence_quality(rent_row)
    return {
        "unit_type": str(rent_row.get("unit_type") or "").strip() or "Unknown",
        "sqft": _as_float(rent_row.get("sq_ft")),
        "face_rent": _as_float(rent_row.get("face_rent")),
        "effective_rent": _as_float(rent_row.get("effective_rent")) or _as_float(rent_row.get("face_rent")),
        "rent_psf": _as_float(rent_row.get("rent_psf")),
        "units_available": _as_int(rent_row.get("units_available")),
        "mom_change": _as_float(rent_row.get("mom_change")),
        "yoy_change": _as_float(rent_row.get("yoy_change")),
        "concession": str(concession_row.get("concession_type") or "").strip() or None,
        "evidence_quality": evidence_quality,
        "source_notes": str(rent_row.get("notes") or "").strip() or None,
    }


def _classify_evidence_quality(rent_row: dict[str, str]) -> str:
    notes = str(rent_row.get("notes") or "").lower()
    if "direct playwright" in notes:
        return "direct_playwright"
    if "direct salvage" in notes or "direct cached" in notes or "rendered_html cache" in notes:
        return "direct_cached"
    if "official" in notes and ("indexed" in notes or "search-indexed" in notes):
        return "official_indexed"
    if "forrent indexed" in notes or "rentcafe indexed" in notes:
        return "official_indexed"
    if "om " in notes or "broker" in notes:
        return "om_floorplan"
    return "tracking_csv_salvage"


def _comp_tier_from_notes(notes: Any) -> str | None:
    """Translate configured comp tiers to the lifecycle schema vocabulary."""
    text = str(notes or "").lower()
    if "tier 1" in text and "premium" in text:
        return "premium"
    if "tier 2" in text and ("mid-market" in text or "mid_market" in text):
        return "mid_market"
    if "tier 3" in text and "value" in text:
        return "value"
    return None


def _infer_bed_bath_from_cohort(cohort: dict[str, Any]) -> tuple[int, float]:
    bedrooms = int(cohort.get("bedrooms") or 0)
    bathrooms = float(cohort.get("bathrooms") or 0.0)
    unit_label = str(cohort.get("unit_type") or cohort.get("cohort_id") or "").strip().lower()
    if bedrooms == 0:
        if unit_label.startswith("studio") or unit_label.startswith("s"):
            bedrooms = 0
            bathrooms = bathrooms or 1.0
        elif unit_label.startswith("a"):
            bedrooms = 1
            bathrooms = bathrooms or 1.0
        elif unit_label.startswith("b"):
            bedrooms = 2
            if bathrooms <= 1.0:
                bathrooms = 2.0
        elif unit_label.startswith("c"):
            bedrooms = 3
            if bathrooms <= 1.0:
                bathrooms = 2.0
    if bathrooms == 0.0:
        bathrooms = 1.0 if bedrooms <= 1 else 2.0
    return bedrooms, bathrooms


def _infer_bed_bath_from_rent_row(row: dict[str, str]) -> tuple[int | None, float | None]:
    beds = _as_int(row.get("beds"))
    baths = _as_float(row.get("baths"))
    if beds is not None and baths is not None:
        return beds, baths

    unit_type = str(row.get("unit_type") or "").strip().lower()
    match = re.search(r"(\d+)\s*(?:br|bed|bd)\s*/?\s*(\d+(?:\.\d+)?)\s*(?:ba|bath|bth)?", unit_type)
    if match:
        return int(match.group(1)), float(match.group(2))

    match = re.search(r"(\d+)\s*(?:br|bed|bd)", unit_type)
    if match:
        parsed_beds = int(match.group(1))
        return parsed_beds, 1.0 if parsed_beds <= 1 else None

    if "studio" in unit_type:
        return 0, baths or 1.0

    return beds, baths


def _should_rebuild_existing_artifacts(provenance_path: Path) -> bool:
    provenance = _load_json(provenance_path) or {}
    scraper_version = str(provenance.get("scraper_version") or "").lower()
    if "salvage" in scraper_version:
        return True
    if isinstance(provenance.get("repo_native_outputs"), dict):
        return True
    provenance_text = json.dumps(provenance).lower()
    if "playwright mcp tools not available" in provenance_text:
        return True
    if "playwright browser fallback not available" in provenance_text:
        return True
    return False


def _build_grouped_payload(
    *,
    canonical: dict[str, Any],
    property_database_rows: list[dict[str, str]],
    rent_rows_latest: dict[str, list[dict[str, str]]],
    reports_dir: Path | None,
) -> dict[str, Any] | None:
    unit_cohorts = canonical.get("unit_cohorts") or []
    if not unit_cohorts:
        return None
    subject_name = str((canonical.get("metadata") or {}).get("deal_id") or "").strip()
    grouped: dict[str, list[dict[str, Any]]] = {}
    notes = [
        "Lifecycle salvage built cohort-grouped comps from market-study-agent tracking CSVs.",
        "Asking rents use the latest rent_history rows available per comp property, including broker OM seed comps and supplemental configured/tracking comps.",
        "Entry source labels distinguish direct_playwright, direct_cached, official_indexed, om_floorplan, and generic tracking_csv_salvage evidence.",
        "Do not treat missing Claude/Codex Playwright MCP tools as a market-study browser blocker; repo-native Python Playwright must be checked separately when direct-site evidence is thin.",
    ]

    comp_meta = {
        str(row.get("property") or "").strip(): row
        for row in property_database_rows
        if str(row.get("property") or "").strip() and str(row.get("property") or "").strip() != subject_name
    }
    rent_candidates: list[dict[str, str]] = []
    for prop, rows in rent_rows_latest.items():
        if prop == subject_name:
            continue
        rent_candidates.extend(rows)

    for cohort in unit_cohorts:
        beds, baths = _infer_bed_bath_from_cohort(cohort)
        sqft = int(round(float(cohort.get("sqft") or 0.0)))
        key = cohort_key(beds, baths, sqft)
        exact = [
            row for row in rent_candidates
            if _infer_bed_bath_from_rent_row(row) == (beds, baths)
        ]
        fallback = [
            row for row in rent_candidates
            if _infer_bed_bath_from_rent_row(row)[0] == beds
        ]
        chosen = exact or fallback
        chosen = sorted(
            chosen,
            key=lambda row: abs((_as_float(row.get("sq_ft")) or 0.0) - float(sqft)),
        )[:8]
        entries: list[dict[str, Any]] = []
        for row in chosen:
            prop = str(row.get("property") or "").strip()
            meta = comp_meta.get(prop, {})
            rent = _as_float(row.get("effective_rent")) or _as_float(row.get("face_rent"))
            row_sqft = _as_float(row.get("sq_ft")) or float(sqft)
            if rent is None:
                continue
            entries.append(
                {
                    "property_name": prop,
                    "address": meta.get("address"),
                    "distance_miles": _as_float(meta.get("distance_mi")),
                    "year_built": _as_int(meta.get("year_built")),
                    "bedrooms": beds,
                    "bathrooms": baths,
                    "sqft": row_sqft,
                    "asking_rent": rent,
                    "rent_per_sqft": round(rent / row_sqft, 2) if row_sqft else None,
                    "source": _classify_evidence_quality(row),
                    "source_url": str(reports_dir / "comps" / "tracking" / "rent_history.csv") if reports_dir else None,
                }
            )
        grouped[key] = entries

    return {
        "subject": {
            "name": subject_name,
            "address": (canonical.get("metadata") or {}).get("address"),
        },
        "as_of": str((canonical.get("metadata") or {}).get("as_of_date") or ""),
        "status": "needs_analyst_input",
        "comps_by_cohort": grouped,
        "methodology_notes": notes,
    }


def _build_comps_payload(
    *,
    canonical: dict[str, Any],
    property_database_rows: list[dict[str, str]],
    rent_rows_latest: dict[str, list[dict[str, str]]],
    concession_rows_latest: dict[str, list[dict[str, str]]],
    snapshot_relative: str | None,
) -> dict[str, Any] | None:
    subject_name = str((canonical.get("metadata") or {}).get("deal_id") or "").strip()
    metadata = canonical.get("metadata") or {}
    market_slug = _normalize_market_slug(metadata.get("market")) or "unknown"
    market_display = _display_market_name(metadata.get("market"), metro_slug=market_slug)
    as_of = None
    comps: list[dict[str, Any]] = []
    concession_by_property = {
        prop: rows[0]
        for prop, rows in concession_rows_latest.items()
        if rows
    }
    for row in property_database_rows:
        name = str(row.get("property") or "").strip()
        if not name or name == subject_name:
            continue
        rent_rows = rent_rows_latest.get(name) or []
        if not rent_rows:
            continue
        if as_of is None:
            as_of = str(rent_rows[0].get("date") or "").strip() or None
        comps.append(
            {
                "comp_id": _slugify(name) or name,
                "name": name,
                "address": str(row.get("address") or "").strip() or "Unknown",
                "distance_miles": _as_float(row.get("distance_mi")),
                "units": _as_int(row.get("units")) or 1,
                "year_built": _as_int(row.get("year_built")),
                "stories": _as_int(row.get("stories")),
                "owner": str(row.get("owner") or "").strip() or None,
                "management_company": str(row.get("management_company") or "").strip() or None,
                "ownership_type": _normalize_ownership_type(row.get("ownership_type")),
                "renovation_status": _normalize_renovation_status(row.get("renovation_status")),
                "renovation_year": _as_int(row.get("renovation_year")),
                "condition_rating": str(row.get("condition_rating") or "").strip() or None,
                "last_sale_date": str(row.get("last_sale_date") or "").strip() or None,
                "last_sale_price": _as_float(row.get("last_sale_price")),
                "last_sale_ppu": _as_float(row.get("last_sale_ppu")),
                "cap_rate_est": _as_float(row.get("cap_rate_est")),
                "tier": _comp_tier_from_notes(row.get("notes")),
                "notes": str(row.get("notes") or "").strip() or None,
                "unit_types": [
                    _build_unit_type_entry(rent_row, concession_by_property)
                    for rent_row in rent_rows
                ],
            }
        )
    if not comps:
        return None
    payload: dict[str, Any] = {
        "subject": {
            "address": metadata.get("address") or "Unknown",
            "submarket": None,
            "metro_slug": market_slug,
            "metro_display": market_display,
        },
        "as_of": as_of or str(metadata.get("as_of_date") or "")[:10],
        "comps": comps,
        "submarket_aggregates": {"comp_count": len(comps)},
    }
    if snapshot_relative:
        payload["raw_snapshot_relative"] = snapshot_relative
    return payload


def _ensure_federation_artifacts(
    *,
    deal_root: Path,
    run_id: str,
) -> None:
    market_study_dir = deal_root / "outputs" / run_id / "market_study"
    comps_dir = deal_root / "outputs" / run_id / "comps"
    grouped_path = market_study_dir / "comps_cohort_grouped.json"
    provenance_path = market_study_dir / "_provenance.json"
    comps_path = comps_dir / "comps.json"
    rebuild_existing = _should_rebuild_existing_artifacts(provenance_path)
    if grouped_path.exists() and comps_path.exists() and provenance_path.exists() and not rebuild_existing:
        return

    subject_context = _find_subject_context(deal_root, run_id)
    canonical = subject_context["canonical"]
    config_path = _find_property_config(
        market_slug=subject_context.get("market_slug"),
        subject_name=subject_context.get("subject_name"),
        subject_address=subject_context.get("subject_address"),
    )
    reports_dir = _find_property_reports_dir(config_path)
    if reports_dir is None:
        return

    tracking_dir = reports_dir / "comps" / "tracking"
    property_database_rows = _load_csv_rows(tracking_dir / "property_database.csv")
    rent_history_rows = _load_csv_rows(tracking_dir / "rent_history.csv")
    concession_history_rows = _load_csv_rows(tracking_dir / "concession_history.csv")
    if not property_database_rows or not rent_history_rows:
        return

    rent_rows_latest = _latest_by_property(rent_history_rows)
    concession_rows_latest = _latest_by_property(concession_history_rows)
    snapshot_path = next(
        iter(sorted(
            (REPO_ROOT / "data" / "public" / "processed" / "comps").glob(
                f"*{_slugify(subject_context.get('subject_name') or '')}*_comps_snapshot.json"
            )
        )),
        None,
    )
    snapshot_relative = None
    if snapshot_path is not None:
        snapshot_relative = f"outputs/{run_id}/comps/snapshot.json"
        if not (comps_dir / "snapshot.json").exists():
            comps_dir.mkdir(parents=True, exist_ok=True)
            (comps_dir / "snapshot.json").write_bytes(snapshot_path.read_bytes())

    if rebuild_existing or not grouped_path.exists():
        grouped_payload = _build_grouped_payload(
            canonical=canonical,
            property_database_rows=property_database_rows,
            rent_rows_latest=rent_rows_latest,
            reports_dir=reports_dir,
        )
        if grouped_payload is not None:
            market_study_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(grouped_path, grouped_payload)

    if rebuild_existing or not comps_path.exists():
        comps_payload = _build_comps_payload(
            canonical=canonical,
            property_database_rows=property_database_rows,
            rent_rows_latest=rent_rows_latest,
            concession_rows_latest=concession_rows_latest,
            snapshot_relative=snapshot_relative,
        )
        if comps_payload is not None:
            comps_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(comps_path, comps_payload)

    if rebuild_existing or not provenance_path.exists():
        provenance_payload = {
            "scraper_version": "market-study-agent/comp-finder salvage",
            "config_used": str(config_path.relative_to(REPO_ROOT)) if config_path else None,
            "repo_native_outputs": {
                "property_database": str((tracking_dir / "property_database.csv").relative_to(REPO_ROOT)),
                "rent_history": str((tracking_dir / "rent_history.csv").relative_to(REPO_ROOT)),
                "concession_history": str((tracking_dir / "concession_history.csv").relative_to(REPO_ROOT)),
            },
            "fallback_reason": (
                "Lifecycle salvage synthesized federation comp artifacts from repo-native tracking outputs "
                "because comp-finder did not materialize run-scoped artifacts directly."
            ),
        }
        market_study_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(provenance_path, provenance_payload)


def _worst_status(*statuses: str) -> str:
    rank = {"ok": 0, "needs_analyst_input": 1, "error": 2}
    return max(statuses, key=lambda s: rank.get(s, 2))


def _derive_grouped_status(grouped_payload: dict[str, Any] | None) -> str:
    if not grouped_payload:
        return "error"
    comps_by_cohort = grouped_payload.get("comps_by_cohort")
    if not isinstance(comps_by_cohort, dict) or not comps_by_cohort:
        return "error"
    status = _normalize_status(grouped_payload.get("status"))
    if status != "error":
        return status
    for rows in comps_by_cohort.values():
        if not isinstance(rows, list):
            return "error"
    return "ok"


def _derive_comps_status(comps_payload: dict[str, Any] | None) -> str:
    if not comps_payload:
        return "error"
    comps = comps_payload.get("comps")
    if not isinstance(comps, list):
        return "error"
    if len(comps) == 0:
        return "error"
    if len(comps) < 3:
        return "needs_analyst_input"
    for comp in comps:
        if not isinstance(comp, dict):
            return "needs_analyst_input"
        unit_types = comp.get("unit_types")
        if not isinstance(unit_types, list) or len(unit_types) == 0:
            return "needs_analyst_input"
    return "ok"


def _build_sanity_flags(grouped_payload: dict[str, Any] | None) -> list[str]:
    if not grouped_payload:
        return ["missing_grouped_comp_artifact"]
    flags: list[str] = []
    comps_by_cohort = grouped_payload.get("comps_by_cohort")
    if not isinstance(comps_by_cohort, dict):
        return ["missing_grouped_comp_artifact"]
    for cohort_key, rows in comps_by_cohort.items():
        if not isinstance(rows, list):
            continue
        if len(rows) < 3:
            flags.append(f"thin_cohort_{cohort_key}_only_{len(rows)}_comps")
    return flags


def _build_comps_quality_flags(comps_payload: dict[str, Any] | None) -> list[str]:
    if not comps_payload:
        return []
    comps = comps_payload.get("comps")
    if not isinstance(comps, list):
        return []
    flags: list[str] = []
    for comp in comps:
        if not isinstance(comp, dict):
            continue
        unit_types = comp.get("unit_types")
        if not isinstance(unit_types, list) or not unit_types:
            continue
        qualities = {
            str(row.get("evidence_quality") or "")
            for row in unit_types
            if isinstance(row, dict)
        }
        if qualities and qualities <= {"om_floorplan"}:
            name = _slugify(str(comp.get("name") or "unknown_comp")) or "unknown_comp"
            flags.append(f"om_only_comp_{name}")
    return flags


def _build_provenance_entries(
    *,
    provenance_payload: dict[str, Any] | None,
    run_id: str,
) -> list[ProvenanceEntry]:
    extracted_at = datetime.now(timezone.utc)
    if not provenance_payload:
        return [
            ProvenanceEntry(
                source="market-study-agent",
                locator=f"outputs/{run_id}/market_study/_provenance.json",
                extracted_at=extracted_at,
                note="Comp-finder artifacts were present but provenance metadata was missing or unreadable.",
            )
        ]

    entries: list[ProvenanceEntry] = [
        ProvenanceEntry(
            source=provenance_payload.get("scraper_version") or "market-study-agent/comp-finder",
            locator=provenance_payload.get("config_used"),
            extracted_at=extracted_at,
            note="Deterministic BridgeResponseV1 emitted from on-disk comp-finder artifacts.",
        )
    ]

    source_breakdown = provenance_payload.get("source_breakdown")
    if isinstance(source_breakdown, dict):
        om_extraction = source_breakdown.get("om_extraction")
        if isinstance(om_extraction, dict):
            entries.append(
                ProvenanceEntry(
                    source="OM extraction fallback",
                    locator=om_extraction.get("source"),
                    extracted_at=extracted_at,
                    note=om_extraction.get("note"),
                )
            )
        web_search = source_breakdown.get("web_search_enrichment")
        if isinstance(web_search, dict):
            sources = web_search.get("sources")
            entries.append(
                ProvenanceEntry(
                    source="web_search_enrichment",
                    locator=", ".join(sources) if isinstance(sources, list) else None,
                    extracted_at=extracted_at,
                    note=web_search.get("note"),
                )
            )

    repo_outputs = provenance_payload.get("repo_native_outputs")
    if isinstance(repo_outputs, dict) and repo_outputs.get("analyst_report"):
        entries.append(
            ProvenanceEntry(
                source="repo-native analyst report",
                locator=repo_outputs.get("analyst_report"),
                extracted_at=extracted_at,
                note="Analyst-grade comp analysis generated inside market-study-agent.",
            )
        )

    return entries


def build_comp_finder_response(
    *,
    deal_root: Path,
    deal_slug: str,
    run_id: str,
) -> BridgeResponseV1:
    _ensure_federation_artifacts(deal_root=deal_root, run_id=run_id)

    market_study_dir = deal_root / "outputs" / run_id / "market_study"
    comps_dir = deal_root / "outputs" / run_id / "comps"

    grouped_path = market_study_dir / "comps_cohort_grouped.json"
    provenance_path = market_study_dir / "_provenance.json"
    comps_path = comps_dir / "comps.json"
    snapshot_path = comps_dir / "snapshot.json"

    grouped_payload = _load_json(grouped_path)
    provenance_payload = _load_json(provenance_path)
    comps_payload = _load_json(comps_path)

    grouped_status = _derive_grouped_status(grouped_payload)
    comps_status = _derive_comps_status(comps_payload)
    final_status = _worst_status(grouped_status, comps_status)
    sanity_flags = _build_sanity_flags(grouped_payload) + _build_comps_quality_flags(comps_payload)
    provenance_entries = _build_provenance_entries(
        provenance_payload=provenance_payload,
        run_id=run_id,
    )

    artifacts: list[ArtifactRef] = []
    if grouped_path.exists():
        artifacts.append(
            ArtifactRef(
                relative_path=f"outputs/{run_id}/market_study/comps_cohort_grouped.json",
                kind="json",
                description="Per-cohort rent comp set, asking-rent only, with source attribution per comp.",
                bytes=grouped_path.stat().st_size,
            )
        )
    if provenance_path.exists():
        artifacts.append(
            ArtifactRef(
                relative_path=f"outputs/{run_id}/market_study/_provenance.json",
                kind="json",
                description="Source breakdown, scraper version, extraction date, blacklisted sources.",
                bytes=provenance_path.stat().st_size,
            )
        )
    if comps_path.exists():
        artifacts.append(
            ArtifactRef(
                relative_path=f"outputs/{run_id}/comps/comps.json",
                kind="json",
                description="Per-comp property attributes + per-unit-type rent rows + submarket aggregates.",
                bytes=comps_path.stat().st_size,
            )
        )
    if snapshot_path.exists():
        artifacts.append(
            ArtifactRef(
                relative_path=f"outputs/{run_id}/comps/snapshot.json",
                kind="json",
                description="ETL snapshot pointer (heterogeneous; not consumed directly by lifecycle).",
                bytes=snapshot_path.stat().st_size,
            )
        )

    if final_status == "error":
        missing = []
        if grouped_status == "error":
            missing.append("market_study/comps_cohort_grouped.json")
        if comps_status == "error":
            missing.append("comps/comps.json")
        return BridgeResponseV1(
            deal_slug=deal_slug,
            run_id=run_id,
            agent_name="comp-finder",
            status="error",
            payload=None,
            artifacts=artifacts,
            provenance=provenance_entries,
            error=BridgeError(
                code="missing_or_invalid_artifact",
                message=(
                    "Comp-finder artifacts are incomplete or invalid; "
                    "cannot construct a federation-ready response envelope."
                ),
                recoverable=False,
                details={"missing_or_invalid": missing},
            ),
            sanity_flags=sanity_flags or ["missing_comp_finder_artifacts"],
        )

    comps_by_cohort = grouped_payload["comps_by_cohort"]
    methodology_notes = list(grouped_payload.get("methodology_notes") or [])
    payload = CompFinderResponse(
        comps_by_cohort=comps_by_cohort,
        comps_relative=f"outputs/{run_id}/comps/comps.json",
        methodology_notes=methodology_notes,
    )

    error = None
    if final_status == "needs_analyst_input":
        error = BridgeError(
            code="thin_comp_set",
            message="One or more comp cohorts are thin and need analyst follow-up.",
            recoverable=True,
            details={"sanity_flags": sanity_flags},
        )

    return BridgeResponseV1(
        deal_slug=deal_slug,
        run_id=run_id,
        agent_name="comp-finder",
        status=final_status,
        payload=payload.model_dump(),
        artifacts=artifacts,
        provenance=provenance_entries,
        error=error,
        sanity_flags=sanity_flags,
    )


def write_and_emit_response(
    *,
    deal_root: Path,
    deal_slug: str,
    run_id: str,
) -> BridgeResponseV1:
    response = build_comp_finder_response(
        deal_root=deal_root,
        deal_slug=deal_slug,
        run_id=run_id,
    )
    envelope_path = deal_root / "outputs" / run_id / "market_study" / "_response_envelope.json"
    _atomic_write_json(envelope_path, response.model_dump(mode="json"))
    print(envelope_path.read_text(encoding="utf-8"))
    return response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and emit the deterministic comp-finder BridgeResponseV1."
    )
    parser.add_argument("--deal-root", required=True, help="Absolute or relative deal root path.")
    parser.add_argument("--deal-slug", required=True, help="Deal slug echoed in the response.")
    parser.add_argument("--run-id", required=True, help="Run id echoed in the response.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    write_and_emit_response(
        deal_root=Path(args.deal_root).resolve(),
        deal_slug=args.deal_slug,
        run_id=args.run_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
