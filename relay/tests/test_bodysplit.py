"""`app.bodysplit`."""

from __future__ import annotations

import random

import pytest

from app.bodysplit import fits, split_body

ALPHABETS = {
    "latin": "abcdefghij klmnop qrstuv wxyz ",
    "cjk": "你好世界今天天气很好我们一起去玩吧 ",
    "emoji": "😀😃🎉🚀 👨‍👩‍👧 é ñ ",
    "mixed": "ab 你好 😀 c\td\n",
}


def _squash(s: str) -> str:
    return "".join(s.split())


@pytest.mark.parametrize("name", ALPHABETS)
@pytest.mark.parametrize(
    "caps", [(1024, None), (320, 160), (50, None), (7, None), (4, 2)], ids=str
)
def test_property_parts_rejoin_fit_and_are_valid_utf8(name: str, caps):
    max_bytes, max_cp = caps
    rng = random.Random(f"{name}{caps}")
    for _ in range(40):
        text = "".join(rng.choice(ALPHABETS[name]) for _ in range(rng.randint(1, 3000)))
        if not text.strip():
            continue
        parts = split_body(text, max_bytes=max_bytes, max_codepoints=max_cp)
        assert parts and all(parts)
        for p in parts:
            assert len(p.encode("utf-8")) <= max_bytes
            if max_cp is not None:
                assert len(p) <= max_cp
            p.encode("utf-8").decode("utf-8")
        assert _squash("".join(parts)) == _squash(text)
        if fits(text, max_bytes, max_cp):
            assert parts == [text]


def test_short_text_is_returned_whole_untouched():
    assert split_body("hello  world ", max_bytes=1024) == ["hello  world "]
    assert split_body("x" * 1024, max_bytes=1024) == ["x" * 1024]


def test_exact_boundaries_bytes_and_codepoints():
    assert len(split_body("x" * 1025, max_bytes=1024)) == 2
    assert split_body("é" * 512, max_bytes=1024) == ["é" * 512]
    assert [len(p) for p in split_body("é" * 513, max_bytes=1024)] == [512, 1]
    assert split_body("y" * 160, max_bytes=320, max_codepoints=160) == ["y" * 160]
    assert [
        len(p) for p in split_body("y" * 161, max_bytes=320, max_codepoints=160)
    ] == [160, 1]
    # 4-byte emoji: 256 fit, 257 do not
    assert [len(p) for p in split_body("😀" * 257, max_bytes=1024)] == [256, 1]


def test_prefers_whitespace_in_the_last_fifth():
    text = (
        "a" * 70 + " " + "b" * 40
    )  # cap 80: the space is at 70 = inside the last 20 % (64..80)
    assert split_body(text, max_bytes=80) == ["a" * 70, "b" * 40]
    far = "a" * 10 + " " + "b" * 100  # space too early: hard cut at 80
    parts = split_body(far, max_bytes=80)
    assert parts[0] == "a" * 10 + " " + "b" * 69 and parts[1] == "b" * 31


def test_never_returns_an_empty_part():
    parts = split_body("a" + " " * 100 + "b" * 10, max_bytes=20)
    assert all(p for p in parts) and _squash("".join(parts)) == "a" + "b" * 10


def test_cjk_is_about_340_chars_per_1024_bytes():
    parts = split_body("你" * 1000, max_bytes=1024)
    assert [len(p) for p in parts] == [341, 341, 318]


def test_rejects_a_cap_too_small_for_one_code_point():
    with pytest.raises(ValueError):
        split_body("abc", max_bytes=3)
