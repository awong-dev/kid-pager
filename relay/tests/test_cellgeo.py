"""`app.cellgeo` (docs/PROTOCOL.md §13.2): provider mapping, the Firestore cache (30-day expiry,
1-day negative cache) and the sanity checks (accuracy > 50 km, exactly 0,0)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app import cellgeo
from app.db.firestore import get_db
from app.store import cells as cells_store
from app.wire import CellInfo


def _cell(**overrides: Any) -> CellInfo:
    obj: dict[str, Any] = {"mcc": "310", "mnc": "410", "tac": 12345, "ci": 87654321, "rsrp": -95}
    obj.update(overrides)
    return CellInfo.model_validate(obj)


@dataclass
class FakeResponse:
    status_code: int
    _json: dict | None = None
    text: str = ""

    def json(self) -> dict:
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELL_GEO_PROVIDER", raising=False)
    monkeypatch.delenv("CELL_GEO_API_KEY", raising=False)


def _backdate(mcc: str, mnc: str, tac: int, ci: int, *, seconds_ago: int) -> None:
    key = cells_store.cache_key(mcc, mnc, tac, ci)
    ref = get_db().collection("cells").document(key)
    data = ref.get().to_dict()
    data["resolvedAt"] = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    ref.set(data)


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="provider_none"),
        pytest.param({"CELL_GEO_PROVIDER": "google"}, id="key_missing"),
        pytest.param(
            {"CELL_GEO_PROVIDER": "not-a-real-provider", "CELL_GEO_API_KEY": "k"}, id="unknown_provider"
        ),
    ],
)
def test_resolve_is_a_noop_without_a_usable_provider_config(monkeypatch: pytest.MonkeyPatch, env: dict):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cellgeo.resolve(_cell()) is None


@pytest.mark.parametrize(
    ("overrides", "signal"),
    [
        pytest.param({}, {"signalStrength": -95}, id="with_rsrp"),
        pytest.param({"ci": 102, "rsrp": None}, {}, id="rsrp_absent"),
    ],
)
def test_google_request_shape_and_response_mapping(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], signal: dict[str, int]
):
    monkeypatch.setenv("CELL_GEO_PROVIDER", "google")
    monkeypatch.setenv("CELL_GEO_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    def fake_post(url: str, *, params=None, json=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        return FakeResponse(200, {"location": {"lat": 1.5, "lng": 2.5}, "accuracy": 30})

    monkeypatch.setattr(cellgeo.httpx, "post", fake_post)

    cell = _cell(**overrides)
    fix = cellgeo.resolve(cell)
    assert fix is not None
    assert fix.lat == 1.5
    assert fix.lon == 2.5
    assert fix.acc_m == 30
    assert fix.provider == "google"

    assert captured["url"] == cellgeo._GOOGLE_URL
    assert captured["params"] == {"key": "test-key"}
    # considerIp: false, otherwise Google locates the relay's IP, not the pager.
    assert captured["json"]["considerIp"] is False
    assert captured["json"]["radioType"] == "lte"
    tower = captured["json"]["cellTowers"][0]
    assert tower == {
        "cellId": cell.ci,
        "locationAreaCode": 12345,
        "mobileCountryCode": 310,
        "mobileNetworkCode": 410,
        **signal,
    }


def _raise_timeout(*a: Any, **k: Any):
    raise httpx.ConnectTimeout("simulated timeout")


@pytest.mark.parametrize(
    "fake_post",
    [
        pytest.param(lambda *a, **k: FakeResponse(404, {"error": {}}), id="404_unknown_cell"),
        pytest.param(lambda *a, **k: FakeResponse(500, {}), id="non_2xx"),
        pytest.param(_raise_timeout, id="transport_failure"),
    ],
)
def test_google_failures_resolve_to_none_and_never_raise(monkeypatch: pytest.MonkeyPatch, fake_post: Any):
    monkeypatch.setenv("CELL_GEO_PROVIDER", "google")
    monkeypatch.setenv("CELL_GEO_API_KEY", "test-key")
    monkeypatch.setattr(cellgeo.httpx, "post", fake_post)
    assert cellgeo.resolve(_cell(ci=103)) is None


def test_opencellid_request_shape_and_response_mapping(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CELL_GEO_PROVIDER", "opencellid")
    monkeypatch.setenv("CELL_GEO_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    def fake_get(url: str, *, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResponse(200, {"lat": 3.0, "lon": 4.0, "range": 500})

    monkeypatch.setattr(cellgeo.httpx, "get", fake_get)

    fix = cellgeo.resolve(_cell())
    assert fix is not None
    assert fix.lat == 3.0
    assert fix.lon == 4.0
    assert fix.acc_m == 500
    assert fix.provider == "opencellid"
    assert captured["url"] == cellgeo._OPENCELLID_URL
    assert captured["params"] == {
        "key": "test-key",
        "mcc": "310",
        "mnc": "410",
        "lac": 12345,
        "cellid": 87654321,
        "format": "json",
    }


@pytest.mark.parametrize(
    "fake_get",
    [
        pytest.param(lambda *a, **k: FakeResponse(200, {"error": "not found"}), id="unknown_cell"),
        pytest.param(_raise_timeout, id="transport_failure"),
    ],
)
def test_opencellid_failures_resolve_to_none_and_never_raise(monkeypatch: pytest.MonkeyPatch, fake_get: Any):
    monkeypatch.setenv("CELL_GEO_PROVIDER", "opencellid")
    monkeypatch.setenv("CELL_GEO_API_KEY", "test-key")
    monkeypatch.setattr(cellgeo.httpx, "get", fake_get)
    assert cellgeo.resolve(_cell(ci=202)) is None


@pytest.mark.parametrize(
    ("lat", "lng", "accuracy", "accepted"),
    [
        pytest.param(1.0, 2.0, 50_001, False, id="accuracy_over_50km"),
        pytest.param(1.0, 2.0, 50_000, True, id="accuracy_at_exactly_50km"),
        pytest.param(0.0, 0.0, 30, False, id="exactly_0_0"),
    ],
)
def test_sanity_checks_on_the_provider_fix(
    monkeypatch: pytest.MonkeyPatch, lat: float, lng: float, accuracy: int, accepted: bool
):
    monkeypatch.setenv("CELL_GEO_PROVIDER", "google")
    monkeypatch.setenv("CELL_GEO_API_KEY", "test-key")
    monkeypatch.setattr(
        cellgeo.httpx,
        "post",
        lambda *a, **k: FakeResponse(200, {"location": {"lat": lat, "lng": lng}, "accuracy": accuracy}),
    )
    assert (cellgeo.resolve(_cell(ci=301)) is not None) is accepted


def test_cache_hit_skips_a_second_http_call(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CELL_GEO_PROVIDER", "google")
    monkeypatch.setenv("CELL_GEO_API_KEY", "test-key")
    calls = {"n": 0}

    def fake_post(*a: Any, **k: Any):
        calls["n"] += 1
        return FakeResponse(200, {"location": {"lat": 5.0, "lng": 6.0}, "accuracy": 100})

    monkeypatch.setattr(cellgeo.httpx, "post", fake_post)
    cell = _cell(ci=401)
    first = cellgeo.resolve(cell)
    second = cellgeo.resolve(cell)
    assert first == second
    assert calls["n"] == 1


@pytest.mark.parametrize(
    ("negative", "ttl_s", "lat"),
    [
        pytest.param(False, cells_store.POSITIVE_TTL_S, 9.0, id="positive_30_days"),
        pytest.param(True, cells_store.NEGATIVE_TTL_S, 1.0, id="negative_1_day"),
    ],
)
def test_cache_entries_expire_after_their_ttl(
    monkeypatch: pytest.MonkeyPatch, negative: bool, ttl_s: int, lat: float
):
    monkeypatch.setenv("CELL_GEO_PROVIDER", "google")
    monkeypatch.setenv("CELL_GEO_API_KEY", "test-key")
    cell = _cell(ci=402)
    if negative:
        cells_store.remember_unknown(cell.mcc, cell.mnc, cell.tac, cell.ci, provider="google")
    else:
        cells_store.remember_resolved(
            cell.mcc, cell.mnc, cell.tac, cell.ci, lat=1.0, lon=1.0, acc_m=10, provider="google"
        )
    _backdate(cell.mcc, cell.mnc, cell.tac, cell.ci, seconds_ago=ttl_s + 1)

    calls = {"n": 0}

    def fake_post(*a: Any, **k: Any):
        calls["n"] += 1
        return FakeResponse(200, {"location": {"lat": lat, "lng": lat}, "accuracy": 20})

    monkeypatch.setattr(cellgeo.httpx, "post", fake_post)
    fix = cellgeo.resolve(cell)
    assert fix is not None
    assert fix.lat == lat
    assert calls["n"] == 1


def test_negative_cache_skips_repeat_calls_within_a_day(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CELL_GEO_PROVIDER", "google")
    monkeypatch.setenv("CELL_GEO_API_KEY", "test-key")
    calls = {"n": 0}

    def fake_post(*a: Any, **k: Any):
        calls["n"] += 1
        return FakeResponse(404, {})

    monkeypatch.setattr(cellgeo.httpx, "post", fake_post)
    cell = _cell(ci=403)
    assert cellgeo.resolve(cell) is None
    assert cellgeo.resolve(cell) is None
    assert calls["n"] == 1
