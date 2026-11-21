"""`app/clockfmt.py`: the `5:21p` formatter."""

from __future__ import annotations

from datetime import UTC, datetime

from app import clockfmt


def _ts(h: int, m: int) -> float:
    return datetime(2026, 11, 17, h, m, tzinfo=UTC).timestamp()


def test_pacific_default(monkeypatch):
    monkeypatch.delenv("RELAY_TIMEZONE", raising=False)
    # PST (UTC-8): 01:21Z on the 18th = 5:21p on the 17th.
    assert (
        clockfmt.clock(datetime(2026, 11, 18, 1, 21, tzinfo=UTC).timestamp()) == "5:21p"
    )
    assert clockfmt.clock(_ts(8, 5)) == "12:05a"
    assert clockfmt.clock(_ts(20, 0)) == "12:00p"
    assert clockfmt.clock(_ts(17, 59)) == "9:59a"


def test_env_zone_and_bad_zone_fallback(monkeypatch):
    monkeypatch.setenv("RELAY_TIMEZONE", "UTC")
    assert clockfmt.clock(_ts(13, 7)) == "1:07p"
    monkeypatch.setenv("RELAY_TIMEZONE", "Not/AZone")
    assert clockfmt.clock(_ts(13, 7), "fam") == "1:07p"
