"""Download/extract text from a PDF for downstream metric parsing.

This is used to pull Q4 2025 (and later) institutional PDF reports into a stable text
representation for repeatable parsing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any


def fetch_bytes(url: str, timeout_s: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "market-study-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        data = response.read()
    if not isinstance(data, bytes | bytearray):
        raise TypeError("Expected bytes response body")
    return bytes(data)


def extract_text_from_pdf(pdf_bytes: bytes, max_pages: int | None = None) -> str:
    try:
        from pdfminer.high_level import extract_text
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "pdfminer.six is required for PDF text extraction. "
            "Install it via `pip install -r requirements.txt`."
        ) from exc

    page_numbers = None if max_pages is None else list(range(max_pages))
    return extract_text(BytesIO(pdf_bytes), page_numbers=page_numbers) or ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract text from a PDF (URL or local file).")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="PDF URL to download.")
    src.add_argument("--path", help="Local PDF path.")
    parser.add_argument("--out-text", help="Write extracted text to this path (UTF-8).")
    parser.add_argument("--out-meta", help="Write extraction metadata JSON to this path.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="If set, only extract the first N pages (faster for large PDFs).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    pdf_bytes: bytes
    source: dict[str, Any]
    if args.url:
        pdf_bytes = fetch_bytes(args.url)
        source = {"type": "url", "value": args.url}
    else:
        path = Path(args.path)
        pdf_bytes = path.read_bytes()
        source = {"type": "path", "value": str(path)}

    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    text = extract_text_from_pdf(pdf_bytes, max_pages=args.max_pages)

    if args.out_text:
        out_text = Path(args.out_text)
        out_text.parent.mkdir(parents=True, exist_ok=True)
        out_text.write_text(text, encoding="utf-8")
    else:
        print(text)

    meta = {
        "source": source,
        "sha256": sha256,
        "text_chars": len(text),
        "max_pages": args.max_pages,
    }
    if args.out_meta:
        out_meta = Path(args.out_meta)
        out_meta.parent.mkdir(parents=True, exist_ok=True)
        out_meta.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
