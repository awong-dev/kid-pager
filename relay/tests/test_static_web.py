"""Static export serving (app/static_web.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import static_web
from app.main import create_app
from tests.fake_transport import FakeBrokerClient
from tests.test_healthz import make_settings

FILES = {
    "index.html": "INDEX",
    "404.html": "NOTFOUND",
    "_next/static/x.js": "JS",
    "firebase-messaging-sw.js": "SW",
    "sw.js": "SW2",
    "manifest.webmanifest": "{}",
}


def build(root: Path, files: dict[str, str] = FILES) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return build(tmp_path / "web")


@pytest.fixture
def client(root: Path) -> TestClient:
    app = FastAPI()

    @app.get("/api/thing")
    def thing() -> dict[str, bool]:
        return {"ok": True}

    static_web.mount(app, root)
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "rel", "status"),
    [
        ("", "index.html", 200),
        ("login", "index.html", 200),
        ("admin/users", "index.html", 200),
        ("chat/kid", "index.html", 200),
        ("chat/view/abc", "index.html", 200),
        ("devices/pgr-1", "index.html", 200),
        ("manifest.webmanifest", "manifest.webmanifest", 200),
        ("_next/static/x.js", "_next/static/x.js", 200),
        ("_next/static/gone.js", "404.html", 404),
    ],
)
def test_resolve(root: Path, path: str, rel: str, status: int):
    found, code = static_web.resolve(root, path)
    assert code == status
    assert found == (root / rel).resolve()


def test_unknown_page_path_is_the_shell_and_a_missing_asset_is_404(client: TestClient):
    r = client.get("/nope")
    assert r.status_code == 200 and r.text == "INDEX"
    r = client.get("/_next/static/gone.js")
    assert r.status_code == 404 and r.text == "NOTFOUND"


def test_without_404_html_resolve_and_http_give_plain_404(tmp_path: Path):
    r = build(tmp_path / "w", {"index.html": "I"})
    assert static_web.resolve(r, "_next/nope") == (None, 404)
    app = FastAPI()
    static_web.mount(app, r)
    resp = TestClient(app).get("/_next/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", ["/api/missing", "/webhooks/x", "/internal/x"])
def test_relay_prefixes_keep_json_404(client: TestClient, path: str):
    r = client.get(path)
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


def test_registered_api_route_still_answers(client: TestClient):
    assert client.get("/api/thing").json() == {"ok": True}


@pytest.mark.parametrize(
    "path",
    ["/../etc/passwd", "/%2e%2e/etc/passwd", "/..%2f..%2fetc/passwd", "/chat/../../x"],
)
def test_path_traversal_never_escapes(tmp_path: Path, path: str):
    (tmp_path / "secret.txt").write_text("SECRET")
    app = FastAPI()
    static_web.mount(app, build(tmp_path / "web", {"index.html": "I"}))
    r = TestClient(app).get(path)
    assert "SECRET" not in r.text
    assert static_web.resolve(tmp_path / "web", "../secret.txt") == (None, 404)


def test_headers_and_content_types(client: TestClient):
    r = client.get("/firebase-messaging-sw.js")
    assert r.headers["Service-Worker-Allowed"] == "/"
    r = client.get("/sw.js")
    assert r.text == "SW2" and r.headers["Service-Worker-Allowed"] == "/"
    assert "service-worker-allowed" not in client.get("/_next/static/x.js").headers
    assert (
        client.get("/manifest.webmanifest")
        .headers["content-type"]
        .startswith("application/manifest+json")
    )


def test_head_works(client: TestClient):
    r = client.head("/family")
    assert r.status_code == 200 and r.text == ""
    assert client.head("/_next/nope").status_code == 404


def test_create_app_mounts_only_when_web_root_exists(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(static_web, "WEB_ROOT", tmp_path / "missing")
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        r = c.get("/family")
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}

    monkeypatch.setattr(static_web, "WEB_ROOT", root)
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        assert c.get("/family").text == "INDEX"
        assert c.get("/healthz").status_code in (200, 503)
