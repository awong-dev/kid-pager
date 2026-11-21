"""Tests for `app/scheduler.py`, the in-process tick/sweep timer."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler
from app.config import Settings
from app.db.firestore import get_db
from app.main import create_app
from app.store import settings as settings_store
from tests.fake_transport import FakeBrokerClient


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "broker_api_url": "http://unused.invalid/api/v5",
        "broker_api_key": None,
        "broker_api_secret": None,
        "webhook_key": "k",
        "dev_mode": True,
        "google_cloud_project": None,
        "firestore_emulator_host": None,
        "firebase_auth_emulator_host": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class _Calls:
    def __init__(self, monkeypatch, *, tick_fails: bool = False, sweep_fails: bool = False):
        self.ticks = 0
        self.sweeps = 0

        def _tick(routing):
            self.ticks += 1
            if tick_fails:
                raise RuntimeError("tick boom")
            return "ticked"

        def _sweep():
            self.sweeps += 1
            if sweep_fails:
                raise RuntimeError("sweep boom")
            settings_store.mark_swept()
            return "swept"

        monkeypatch.setattr(jobs, "tick", _tick)
        monkeypatch.setattr(jobs, "sweep", _sweep)


def test_first_slot_ticks_and_sweeps_a_never_swept_store(monkeypatch):
    calls = _Calls(monkeypatch)
    scheduler.run_once(routing=None)  # type: ignore[arg-type]
    assert (calls.ticks, calls.sweeps) == (1, 1)
    meta = settings_store.get_meta()
    assert meta.lastTickAt is not None and meta.lastSweepAt is not None


def test_a_recent_sweep_is_not_repeated_and_a_day_old_one_is(monkeypatch):
    calls = _Calls(monkeypatch)
    settings_store.mark_swept()
    scheduler.run_once(routing=None)  # type: ignore[arg-type]
    assert (calls.ticks, calls.sweeps) == (1, 0)
    day_later = datetime.now(UTC) + timedelta(hours=23, minutes=1)
    scheduler.run_once(routing=None, now=day_later)  # type: ignore[arg-type]
    assert calls.sweeps == 1


def test_a_failing_tick_is_logged_and_the_sweep_still_runs(monkeypatch, caplog):
    calls = _Calls(monkeypatch, tick_fails=True)
    scheduler.run_once(routing=None)  # type: ignore[arg-type]
    assert calls.sweeps == 1
    assert "scheduled tick failed" in caplog.text
    assert settings_store.get_meta().lastTickAt is None


def test_a_failing_sweep_is_retried_on_the_next_slot(monkeypatch, caplog):
    calls = _Calls(monkeypatch, sweep_fails=True)
    scheduler.run_once(routing=None)  # type: ignore[arg-type]
    scheduler.run_once(routing=None)  # type: ignore[arg-type]
    assert calls.sweeps == 2
    assert "scheduled sweep failed" in caplog.text


def test_run_waits_30_s_then_every_5_min(monkeypatch):
    calls = _Calls(monkeypatch)
    delays: list[float] = []

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run(None, sleep=_sleep))  # type: ignore[arg-type]
    assert delays == [30.0, 300.0, 300.0]
    assert calls.ticks == 2


def test_the_lifespan_runs_the_scheduler_only_when_internal(monkeypatch):
    monkeypatch.setattr(scheduler, "FIRST_TICK_DELAY_S", 0.0)
    calls = _Calls(monkeypatch)
    with TestClient(create_app(settings=_settings(), broker_client=FakeBrokerClient())):
        time.sleep(0.3)
    assert calls.ticks == 0
    with TestClient(
        create_app(settings=_settings(scheduler_mode="internal"), broker_client=FakeBrokerClient())
    ):
        deadline = time.monotonic() + 5
        while calls.ticks == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
    assert calls.ticks == 1
    assert get_db().collection("settings").document("meta").get().exists
