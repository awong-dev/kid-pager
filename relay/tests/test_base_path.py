"""The mount prefix handled by the relay itself (app/base_path.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.fake_transport import FakeBrokerClient
from tests.test_healthz import make_settings


@pytest.fixture
def client():
    settings = make_settings(public_base_url="https://home.example:24492/kid-pager")
    with TestClient(create_app(settings=settings, broker_client=FakeBrokerClient())) as c:
        yield c


def test_a_request_under_the_prefix_is_routed_without_it(client: TestClient):
    bare = client.get("/healthz")
    prefixed = client.get("/kid-pager/healthz")
    assert prefixed.status_code == bare.status_code
    assert prefixed.json() == bare.json()


def test_the_bare_prefix_redirects_to_its_slash_and_keeps_the_query(client: TestClient):
    resp = client.get("/kid-pager?next=x", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/kid-pager/?next=x"


def test_internal_routes_are_refused_under_the_prefix(client: TestClient):
    assert client.post("/kid-pager/internal/tick").status_code == 404
    assert client.post("/internal/tick").status_code != 404


def test_a_path_that_only_starts_like_the_prefix_is_not_stripped(client: TestClient):
    assert client.get("/kid-pagerx/healthz").status_code == 404


def test_no_prefix_means_no_middleware():
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    assert TestClient(app).get("/healthz").status_code in (200, 503)
