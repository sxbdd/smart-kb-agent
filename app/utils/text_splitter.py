"""文本切分：按段落聚合，超长段落按句子边界切分，并保留 overlap。"""
from __future__ import annotations

import re
from typing import List

_SENT_BOUNDARY = re.compile(r"[。！？!?；;\n]")


def split_text(text: str, chunk_size: int = 512, overlap: int = 50) -> List[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须满足 0 <= overlap < chunk_size")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: List[str] = []
    current = ""

    def flush(item: str) -> None:
        nonlocal current
        if item.strip():
            chunks.append(item.strip())
        current = ""

    for para in paragraphs:
        if len(para) > chunk_size:
            tail = current[-overlap:] if current and overlap > 0 else ""
            if current:
                chunks.append(current)
                current = ""
            pieces = _chunk_long(para, chunk_size, overlap)
            if tail and pieces:
                pieces[0] = (tail + "\n\n" + pieces[0]).strip()
            chunks.extend(pieces)
            continue

        if not current:
            current = para
        elif len(current) + 2 + len(para) <= chunk_size:
            current += "\n\n" + para
        else:
            flush(current)
            tail = current[-overlap:] if overlap > 0 else ""
            current = (tail + "\n\n" + para) if tail else para

    flush(current)
    return [c for c in chunks if c.strip()]


def _chunk_long(text: str, chunk_size: int, overlap: int) -> List[str]:
    """切分单个超长文本，尽量在句子边界断开。"""
    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            window = text[start:end]
            best = -1
            for m in _SENT_BOUNDARY.finditer(window):
                best = m.end()
            if best > chunk_size * 0.5:
                end = start + best
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks
