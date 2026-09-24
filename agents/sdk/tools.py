"""SDK @tool wiring for the market-study agent.

Phase 5: each tool dispatches to a real ETL helper. The wiring is intentionally
thin — discovery helpers (``load_comps_snapshot``, ``normalize_csv_string``) live
in this module so unit tests can monkeypatch them without touching the heavy
``etl/`` modules.

Source attribution is enforced at every tool boundary: every comp row carries a
``source`` key, and every generated section quotes its evidence sources. This
matches the governance hook in ``agents/sdk/governance.py``.
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

# Repo-root anchored snapshot directory (matches `etl/collect_comps_snapshot.py`
# output convention: ``data/public/processed/comps/{date}_{metro_slug}_comps_snapshot.json``).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPS_DIR = _REPO_ROOT / "data" / "public" / "processed" / "comps"


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _fail(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


# ---------------------------------------------------------------------------
# Discovery helpers (testable, monkeypatchable from unit tests)
# ---------------------------------------------------------------------------


def _slug_matches_metro(slug: str, metro_key: str) -> bool:
    """Match metro as the first token of the slug (after the date prefix).

    Snapshot filenames are ``YYYY-MM-DD_{metro}_{rest...}_comps_snapshot.json``.
    The metro is the FIRST underscore-separated token of the slug. A naive
    substring check (``metro_key in slug``) leaks across metros — e.g.
    ``metro="dall"`` would silently grab ``dallas_*`` snapshots, and
    ``metro="dallas"`` would grab anything containing the word "dallas"
    elsewhere in the slug. Token equality on the leading position prevents
    both classes of leak.
    """
    if not metro_key:
        return True
    parts = slug.lower().split("_", 1)
    return parts[0] == metro_key.lower()


def _coerce_evidence_to_rows(evidence: list) -> list[dict[str, Any]]:
    """Accept either a list of dicts or a single JSON-encoded payload string.

    The agent receives ``pull_comps``'s JSON payload as a text content block.
    The cleanest path is for the agent to pass that exact string back as the
    single evidence entry to ``generate_section``. This helper decodes it so
    the renderer sees N rows instead of one opaque string.
    """
    if not evidence:
        return []
    # Single JSON-encoded entry: {"rows": [...], ...} envelope or [...] list
    if len(evidence) == 1 and isinstance(evidence[0], str):
        try:
            payload = json.loads(evidence[0])
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            return [r for r in payload["rows"] if isinstance(r, dict)]
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
    # Otherwise assume already a list of dicts
    return [e for e in evidence if isinstance(e, dict)]


def load_comps_snapshot(metro: str, asset_class: str, limit: int) -> list[dict[str, Any]]:
    """Load the most recent comps snapshot whose metro_slug matches ``metro``.

    Replaces the placeholder ``etl.collect_comps_snapshot.fetch_comps`` reference
    in the merged Phase-5 stub. ``collect_comps_snapshot.py`` is a CLI-driven
    pipeline that writes JSON snapshots; this helper consumes those snapshots
    rather than re-running scraping. Each returned row gets a ``source`` key
    of the form ``snapshot:<metro_slug>:<run_date>`` so the governance hook
    (which requires ``source`` on every evidence row) accepts it.
    """
    if not _COMPS_DIR.exists():
        return []

    metro_key = (metro or "").lower().strip()
    candidates: list[tuple[str, Path]] = []
    for path in _COMPS_DIR.glob("*_comps_snapshot.json"):
        # Filename pattern: YYYY-MM-DD_{metro_slug}_comps_snapshot.json
        stem = path.stem  # drops .json
        # Date is the first 10 chars; slug is between "_" and "_comps_snapshot"
        if len(stem) < 11 or stem[10] != "_":
            continue
        run_date = stem[:10]
        slug = stem[11:].rsplit("_comps_snapshot", 1)[0]
        if not _slug_matches_metro(slug, metro_key):
            continue
        candidates.append((run_date, path))

    if not candidates:
        return []

    # Most recent first
    candidates.sort(key=lambda t: t[0], reverse=True)
    run_date, path = candidates[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    metro_slug = payload.get("metro_slug") or path.stem[11:].rsplit("_comps_snapshot", 1)[0]

    rows: list[dict[str, Any]] = []
    for comp in payload.get("comps", []) or []:
        row = {
            "name": comp.get("name"),
            "address": comp.get("address"),
            "direct_floorplans_url": comp.get("direct_floorplans_url"),
            "apartments_com_url": comp.get("apartments_com_url"),
            "available_units": ((comp.get("direct") or {}).get("available_units") or []),
            "source": f"snapshot:{metro_slug}:{run_date}",
        }
        rows.append(row)

    if limit and limit > 0:
        rows = rows[:limit]
    return rows


def normalize_csv_string(csv_text: str) -> list[dict[str, Any]]:
    """Normalize a raw rent-roll CSV string to the canonical schema.

    Replaces the placeholder ``etl.standardize_rent_roll.normalize_csv`` reference
    in the merged Phase-5 stub. The real ``standardize_rent_roll.transform_rent_roll``
    operates on a Path; this helper writes the CSV string to a temp file,
    auto-detects the mapping config from headers, and dispatches.
    Returns the list of canonical-schema dicts (may be empty if format is
    unrecognized — caller decides whether to fail loud).
    """
    # Local import keeps SDK module import-fast.
    from etl import standardize_rent_roll as _srr

    text = csv_text.strip()
    if not text:
        return []

    # Read headers so we can auto-detect the mapping config.
    reader = csv.reader(io.StringIO(text))
    header_row = next(reader, None)
    if not header_row:
        return []
    headers = [str(h).strip() for h in header_row]

    config_name = _srr.detect_config(headers)
    if not config_name:
        return []

    try:
        config = _srr.load_config(config_name)
    except FileNotFoundError:
        return []

    # transform_rent_roll wants a Path; write the CSV to a temp file.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
    ) as tmp:
        tmp.write(text)
        if not text.endswith("\n"):
            tmp.write("\n")
        tmp_path = Path(tmp.name)

    try:
        return _srr.transform_rent_roll(tmp_path, config)
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_comp_set_overview(evidence: list[Any]) -> str:
    """Render the 'Comp Set Overview' markdown section from evidence rows.

    Mirrors the 'Competitive Set Summary' table format in
    ``.claude/skills/pull-comps/SKILL.md`` but kept minimal: no fabrication,
    every row's ``source`` is preserved. Accepts either a list of row dicts
    or a single JSON-encoded payload string (defensive — the agent typically
    passes the raw ``pull_comps`` output through).
    """
    rows = _coerce_evidence_to_rows(evidence)
    lines = ["## Comp Set Overview", ""]
    lines.append("| # | Property | Address | Units Avail | Min Rent | Max Rent | Source |")
    lines.append("|:-:|----------|---------|------------:|---------:|---------:|--------|")
    for i, row in enumerate(rows, start=1):
        name = row.get("name") or "—"
        address = row.get("address") or "—"
        units_avail = row.get("units_available")
        if units_avail is None:
            avail_units = row.get("available_units") or []
            units_avail = len(avail_units) if isinstance(avail_units, list) else "—"
        min_rent = row.get("min_rent")
        max_rent = row.get("max_rent")
        if (min_rent is None or max_rent is None) and isinstance(
            row.get("available_units"), list
        ):
            rents = [u.get("rent") for u in row["available_units"] if u.get("rent") is not None]
            if rents:
                min_rent = min_rent if min_rent is not None else min(rents)
                max_rent = max_rent if max_rent is not None else max(rents)
        source = row.get("source") or "—"
        lines.append(
            f"| {i} | {name} | {address} | {units_avail} | "
            f"{_fmt_money(min_rent)} | {_fmt_money(max_rent)} | {source} |"
        )
    return "\n".join(lines)


def _render_generic_grounded(section: str, evidence: list[dict[str, Any]]) -> str:
    """Fallback for sections without a dedicated renderer. Lists evidence rows
    with their sources — never fabricates."""
    lines = [f"## {section}", "", f"Grounded in {len(evidence)} evidence row(s):", ""]
    for i, row in enumerate(evidence, start=1):
        name = row.get("name") or row.get("title") or f"row {i}"
        source = row.get("source") or "—"
        lines.append(f"- {name} (source: {source})")
    return "\n".join(lines)


def _fmt_money(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


_SECTION_RENDERERS: dict[str, Any] = {
    "comp set overview": _render_comp_set_overview,
}


# ---------------------------------------------------------------------------
# @tool wrappers
# ---------------------------------------------------------------------------


@tool(
    "pull_comps",
    "Pull comp set for a metro+asset class with source attribution",
    {"metro": str, "asset_class": str, "limit": int},
)
async def pull_comps_tool(args: dict[str, Any]) -> dict[str, Any]:
    metro = args.get("metro", "")
    asset_class = args.get("asset_class", "")
    limit = int(args.get("limit") or 0)

    rows = load_comps_snapshot(metro=metro, asset_class=asset_class, limit=limit)
    if not rows:
        return _fail(
            f"FAIL: no comps snapshot for metro='{metro}'. "
            f"Run /pull-comps with a configured property first."
        )
    sourced = [r for r in rows if r.get("source")]
    if len(sourced) != len(rows):
        return _fail(
            f"FAIL: {len(rows) - len(sourced)} rows missing source attribution"
        )
    sources = sorted({str(r["source"]) for r in rows})
    payload = {
        "row_count": len(rows),
        "rows": rows,
        "sources": sources,
    }
    return _ok(json.dumps(payload, indent=2, default=str))


@tool(
    "normalize_rent_roll",
    "Normalize a raw rent-roll CSV to canonical schema",
    {"rent_roll_csv": str},
)
async def normalize_rent_roll_tool(args: dict[str, Any]) -> dict[str, Any]:
    csv_text = args.get("rent_roll_csv", "")
    if not csv_text or not csv_text.strip():
        return _fail("FAIL: empty rent_roll_csv")

    records = normalize_csv_string(csv_text)
    if not records:
        return _fail(
            "FAIL: rent roll format not recognized "
            "(no auto-detected mapping in configs/rent_roll_mappings/)"
        )
    return _ok(f"normalized rent_roll OK records={len(records)}")


@tool(
    "generate_section",
    "Write a market study section grounded in evidence",
    {"section": str, "evidence": list},
)
async def generate_section_tool(args: dict[str, Any]) -> dict[str, Any]:
    section = args.get("section", "")
    evidence = args.get("evidence") or []
    if not evidence:
        return _fail(
            "FAIL: cannot generate section with empty evidence (data governance)"
        )

    renderer = _SECTION_RENDERERS.get(section.lower().strip())
    if renderer is None:
        body = _render_generic_grounded(section, evidence)
    else:
        body = renderer(evidence)
    return _ok(body)
