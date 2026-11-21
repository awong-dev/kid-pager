"""GSM-7 / UCS-2 segment counting (`app/carriers/segments.py`)."""

from __future__ import annotations

from app.carriers.segments import count_segments, is_gsm7


def test_gsm7_single_and_multi_part_boundaries():
    assert count_segments("") == 1
    assert count_segments("a" * 160) == 1
    assert count_segments("a" * 161) == 2
    assert count_segments("a" * 306) == 2  # 2 x 153
    assert count_segments("a" * 307) == 3


def test_extension_characters_cost_two_septets():
    assert is_gsm7("price: 5 € [ok]")
    assert count_segments("{" * 80) == 1  # 160 septets
    assert count_segments("{" * 81) == 2  # 162 septets
    assert count_segments("€" + "a" * 158) == 1  # 160
    assert count_segments("€" + "a" * 159) == 2  # 161


def test_an_escape_pair_is_not_split_across_parts():
    # 152 septets, then a 2-septet character: it does not fit the 153 and starts part 2.
    text = "a" * 152 + "€" + "a" * 160
    assert count_segments(text) == 3
    # Without the straddle: 153 + 153 exactly.
    assert count_segments("a" * 153 + "{" + "a" * 150) == 2


def test_ucs2_when_any_character_is_outside_gsm7():
    assert not is_gsm7("héllo ☃")
    assert count_segments("☃" * 70) == 1
    assert count_segments("☃" * 71) == 2
    assert count_segments("☃" * 134) == 2  # 2 x 67
    assert count_segments("☃" * 135) == 3


def test_emoji_is_two_utf16_units_and_never_split():
    assert count_segments("😀" * 35) == 1  # 70 units
    assert count_segments("😀" * 36) == 2
    # 66 BMP units then an emoji (2 units) does not fit 67: it starts the next part.
    assert count_segments("☃" * 66 + "😀" + "☃" * 70) == 3


def test_basic_gsm_accents_and_newlines_stay_gsm7():
    assert is_gsm7("Café ñ ü Ø\n\r")
    assert count_segments("é" * 160) == 1
    assert not is_gsm7("ç")  # lowercase c-cedilla is not in the default alphabet
