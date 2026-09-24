"""Merge subagent LLM extractions back into a comp snapshot.

Two-pass workflow when the Anthropic SDK is unavailable (Claude.ai
subscription only). Pass 1 = the snapshot run; pass 2 = a Claude Code
session (or any LLM-equipped runtime) writing structured Shape-B JSON
into the queue dir, then this script merging it back.

Queue layout::

    data/cache/llm_extraction_queue/<date>_<metro_slug>/
        <comp_slug>.html       # rendered floorplans page (snapshot writes)
        <comp_slug>.json       # Shape-B extraction (subagent writes)

Run::

    uv run python etl/merge_llm_extractions.py \\
        --snapshot data/public/processed/comps/<date>_<slug>_comps_snapshot.json \\
        --queue-dir data/cache/llm_extraction_queue/<date>_<slug>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from etl.comp_scraping import normalize_shape


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def merge_snapshot(snapshot_path: Path, queue_dir: Path) -> dict[str, Any]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    merged = 0
    for comp in snapshot.get("comps") or []:
        slug = _slugify(comp.get("name") or "")
        extraction_path = queue_dir / f"{slug}.json"
        if not extraction_path.exists():
            continue
        try:
            extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        normalised = normalize_shape(extraction)
        if not normalised.get("floorplans"):
            continue

        existing = comp.get("direct") or {}
        meta = dict(existing.get("scrape_meta") or {})
        meta["llm_extracted"] = True
        meta["llm_source"] = str(extraction_path.name)
        meta["passes_gate"] = True
        meta["confidence"] = extraction.get("confidence") or "medium"
        # Preserve any specials the engine cascade managed to scrape.
        existing_specials = list(existing.get("specials") or [])
        new_specials = list(normalised.get("specials") or [])
        all_specials = list(dict.fromkeys(existing_specials + new_specials))

        comp["direct"] = {
            "platform": normalised.get("platform") or "llm_extracted",
            "floorplans": normalised["floorplans"],
            "units": normalised.get("units") or [],
            "specials": all_specials,
            "scrape_meta": meta,
        }
        merged += 1

    snapshot.setdefault("merge_meta", {})["llm_merges_applied"] = merged
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--queue-dir", required=True)
    args = parser.parse_args()

    snapshot_path = Path(args.snapshot).resolve()
    queue_dir = Path(args.queue_dir).resolve()

    merged = merge_snapshot(snapshot_path, queue_dir)
    snapshot_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(
        f"merged {merged.get('merge_meta', {}).get('llm_merges_applied', 0)} "
        f"LLM extractions into {snapshot_path.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
