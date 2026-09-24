"""Build or refresh retrieval indices for the Market Study Agent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from retrieval.chunkers import chunk_text

POLICY_PATH = Path("retrieval/policies.yaml")
INDEX_MANIFEST = Path("retrieval/stores/index_manifest.json")


def list_documents(inputs: list[str]) -> list[Path]:
    documents: list[Path] = []
    for pattern in inputs:
        documents.extend(Path().glob(pattern))
    return [doc for doc in documents if doc.is_file()]


def should_exclude(doc: Path, policy: dict[str, Any], text: str | None = None) -> bool:
    exclusions = policy.get("exclude_from_index", {})
    providers = {provider.lower() for provider in exclusions.get("providers", [])}
    keywords = {keyword.lower() for keyword in exclusions.get("keywords", [])}

    doc_path = str(doc).lower()
    if any(provider in doc_path for provider in providers):
        return True
    if any(keyword in doc_path for keyword in keywords):
        return True
    if text:
        lower_text = text.lower()
        if any(keyword in lower_text for keyword in keywords):
            return True
    return False


def build_index(documents: list[Path], policy: dict[str, Any]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for doc in documents:
        paid_content = any(part in doc.parts for part in ("paid",))
        if paid_content and not policy.get("allow_verbatim_from_paid"):
            continue
        if should_exclude(doc, policy):
            continue
        text = doc.read_text(encoding="utf-8")
        if should_exclude(doc, policy, text):
            continue
        index[str(doc)] = chunk_text(text)
    return index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build retrieval indices")
    parser.add_argument(
        "--inputs",
        nargs="+",
        default=["templates/**/*.md", "data/public/processed/**/*.jsonl"],
        help="Glob patterns for documents to index.",
    )
    parser.add_argument(
        "--output",
        default=str(INDEX_MANIFEST),
        help="Manifest path for stored index metadata.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    documents = list_documents(args.inputs)
    index = build_index(documents, policy)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(index, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
