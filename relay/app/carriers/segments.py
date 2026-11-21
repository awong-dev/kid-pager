"""SMS segment counting: GSM 03.38 7-bit is 160/153 per part, UCS-2 is 70/67."""

from __future__ import annotations

# 03.38 default alphabet, one septet each (the escape 0x1B is not a character).
GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_BASIC_SET = frozenset(GSM7_BASIC) - {"\x1b"}
# Reached through the escape: two septets each.
GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")

SINGLE_GSM = 160
MULTI_GSM = 153
SINGLE_UCS2 = 70
MULTI_UCS2 = 67


def _septets(ch: str) -> int | None:
    if ch in GSM7_BASIC_SET:
        return 1
    if ch in GSM7_EXTENSION:
        return 2
    return None


def is_gsm7(text: str) -> bool:
    return all(_septets(ch) is not None for ch in text)


def _pack(units: list[int], single: int, multi: int) -> int:
    """Parts for a sequence of atoms of 1 or 2 units that must not be split."""
    total = sum(units)
    if total <= single:
        return 1
    parts, used = 1, 0
    for u in units:
        if used + u > multi:
            parts += 1
            used = 0
        used += u
    return parts


def count_segments(text: str) -> int:
    """At least 1 (an empty text still occupies a part)."""
    if not text:
        return 1
    if is_gsm7(text):
        return _pack([_septets(ch) or 1 for ch in text], SINGLE_GSM, MULTI_GSM)
    # UCS-2: a character outside the BMP is a surrogate pair, two units.
    return _pack([2 if ord(ch) > 0xFFFF else 1 for ch in text], SINGLE_UCS2, MULTI_UCS2)
