"""Split a long text body into pager-sized parts on code point boundaries, preferring whitespace."""

from __future__ import annotations

PREFER_TAIL = 0.2


def _fit(text: str, max_bytes: int, max_codepoints: int | None) -> int:
    """How many leading code points of `text` fit the caps."""
    used = 0
    limit = len(text) if max_codepoints is None else min(len(text), max_codepoints)
    for i in range(limit):
        used += len(text[i].encode("utf-8", errors="surrogatepass"))
        if used > max_bytes:
            return i
    return limit


def fits(text: str, max_bytes: int, max_codepoints: int | None = None) -> bool:
    return _fit(text, max_bytes, max_codepoints) == len(text)


def split_body(
    text: str, *, max_bytes: int, max_codepoints: int | None = None
) -> list[str]:
    """`[text]` when it fits; otherwise consecutive non-empty parts."""
    if max_bytes < 4:
        raise ValueError("max_bytes must hold at least one code point")
    parts: list[str] = []
    rest = text
    while rest:
        n = _fit(rest, max_bytes, max_codepoints)
        if n == len(rest):
            parts.append(rest)
            break
        cut = n
        start = max(1, int(n * (1 - PREFER_TAIL)))
        for i in range(n - 1, start - 1, -1):
            if rest[i].isspace() and rest[:i].strip():
                cut = i
                break
        part = rest[:cut].rstrip() if cut < n else rest[:cut]
        if not part:  # all blanks before the cut: hard cut, never an empty part
            part, cut = rest[:n], n
        parts.append(part)
        rest = rest[cut:].lstrip() if cut < n else rest[cut:]
    return parts or [text]
