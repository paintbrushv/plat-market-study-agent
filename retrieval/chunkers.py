"""Chunking utilities for retrieval-ready documents."""

from __future__ import annotations

import re

MAX_CHUNK_SIZE = 800  # characters
OVERLAP = 120


def sanitize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def chunk_text(text: str, max_chunk_size: int = MAX_CHUNK_SIZE) -> list[str]:
    clean_text = sanitize(text)
    if len(clean_text) <= max_chunk_size:
        return [clean_text]

    chunks: list[str] = []
    start = 0
    while start < len(clean_text):
        end = min(len(clean_text), start + max_chunk_size)
        chunks.append(clean_text[start:end])
        if end >= len(clean_text):
            break
        start = max(0, end - OVERLAP)
    return chunks
