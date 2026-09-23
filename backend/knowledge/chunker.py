"""Recursive character chunking, with markdown heading carry-over."""
from __future__ import annotations

import re

from backend.app.config import settings

SEPARATORS = ["\n\n", "\n", ". ", " "]
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _split(text: str, size: int, seps: list[str]) -> list[str]:
    if len(text) <= size:
        return [text] if text.strip() else []
    if not seps:
        return [text[i : i + size] for i in range(0, len(text), size)]

    sep, rest = seps[0], seps[1:]
    pieces = text.split(sep)
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        candidate = piece if not buf else buf + sep + piece
        if len(candidate) <= size:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        if len(piece) > size:
            chunks.extend(_split(piece, size, rest))
            buf = ""
        else:
            buf = piece
    if buf.strip():
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    if overlap <= 0 or len(chunks) < 2:
        return chunks
    out = [chunks[0]]
    for prev, cur in zip(chunks, chunks[1:]):
        tail = prev[-overlap:]
        out.append(f"{tail}\n{cur}" if tail.strip() else cur)
    return out


def chunk_text(text: str, *, markdown: bool = False, size: int | None = None, overlap: int | None = None) -> list[str]:
    size = size or settings.chunk_size
    overlap = settings.chunk_overlap if overlap is None else overlap
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if not markdown:
        return _apply_overlap(_split(text, size, SEPARATORS), overlap)
    return _chunk_markdown(text, size, overlap)


def _chunk_markdown(text: str, size: int, overlap: int) -> list[str]:
    """Split on headings first, then by size, prefixing each chunk with its heading path."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    path: list[str] = []
    for line in text.split("\n"):
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            path = path[: level - 1] + [m.group(2).strip()]
            sections.append((" > ".join(path), [line]))
        else:
            sections[-1][1].append(line)

    chunks: list[str] = []
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        for part in _apply_overlap(_split(body, size, SEPARATORS), overlap):
            prefix = f"[{heading}]\n" if heading and not part.lstrip().startswith("#") else ""
            chunks.append(prefix + part)
    return chunks
