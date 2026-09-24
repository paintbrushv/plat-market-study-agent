"""CLI entrypoint for running the Market Study Agent locally."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import yaml

from agents.runners.adapters.chatgpt_webrun import run_agent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Market Study Agent runner")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the metro configuration YAML file.",
    )
    parser.add_argument(
        "--date",
        help="ISO date for the report (defaults to today).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="If set, do not write outputs; just print summary to stdout.",
    )
    return parser.parse_args()


def slugify(value: str) -> str:
    """Generate a filesystem-friendly slug."""
    normalized = "".join(char for char in value.lower() if char.isalnum() or char == " ")
    return normalized.replace(" ", "_")


def resolve_output_path(cfg: dict[str, Any], report_date: str, metro_slug: str) -> Path:
    template = cfg["outputs"]["report_path"]
    return Path(template.format(date=report_date, metro_slug=metro_slug))


def resolve_provenance_path(cfg: dict[str, Any], report_date: str, metro_slug: str) -> Path:
    template = cfg["outputs"].get("provenance_log") or f"reports/logs/build_{report_date}.jsonl"
    return Path(template.format(date=report_date, metro_slug=metro_slug))


def main() -> None:
    args = parse_args()
    report_date = args.date or dt.date.today().isoformat()

    cfg_path = Path(args.config)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    metro_slug = cfg.get("notes", {}).get("metro_slug") or slugify(cfg["metro"])

    template_path = Path("templates/MarketStudy.md")
    template_md = template_path.read_text(encoding="utf-8")

    report_md, provenance = run_agent(
        config=cfg,
        template=template_md,
        run_date=report_date,
    )

    if args.dry_run:
        print(report_md[:1000])
        return

    output_path = resolve_output_path(cfg, report_date, metro_slug)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_md, encoding="utf-8")

    provenance_path = resolve_provenance_path(cfg, report_date, metro_slug)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(provenance, list | tuple):
        lines = [json.dumps(item) for item in provenance]
        provenance_payload = "\n".join(lines)
    else:
        provenance_payload = json.dumps(provenance)

    provenance_path.write_text(provenance_payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
