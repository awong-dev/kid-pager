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
    "family.html": "FAMILY",
    "chat/_.html": "CHAT",
    "chat/view/_.html": "CHATVIEW",
    "devices/_.html": "DEVICES",
    "chat/_.txt": "CHATTXT",
    "_next/static/x.js": "JS",
    "firebase-messaging-sw.js": "SW",
    "sw.js": "SW2",
    "manifest.webmanifest": "{}",
    "admin/users.html": "USERS",
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
        ("family", "family.html", 200),
        ("admin/users", "admin/users.html", 200),
        ("chat/kid", "chat/_.html", 200),
        ("chat/view/abc", "chat/view/_.html", 200),
        ("devices/pgr-1", "devices/_.html", 200),
        ("chat/_.txt", "chat/_.txt", 200),
        ("_next/static/x.js", "_next/static/x.js", 200),
        ("nope", "404.html", 404),
    ],
)
def test_resolve(root: Path, path: str, rel: str, status: int):
    found, code = static_web.resolve(root, path)
    assert code == status
    assert found == (root / rel).resolve()


def test_resolve_without_404_html(tmp_path: Path):
    r = build(tmp_path / "w", {"index.html": "I"})
    assert static_web.resolve(r, "nope") == (None, 404)


def test_pages_over_http(client: TestClient):
    assert client.get("/").text == "INDEX"
    assert client.get("/family").text == "FAMILY"
    assert client.get("/admin/users").text == "USERS"
    assert client.get("/chat/kid").text == "CHAT"
    assert client.get("/chat/view/abc").text == "CHATVIEW"
    assert client.get("/devices/pgr-1").text == "DEVICES"
    assert client.get("/chat/_.txt").text == "CHATTXT"


def test_unknown_path_is_404_with_404_html(client: TestClient):
    r = client.get("/nope")
    assert r.status_code == 404 and r.text == "NOTFOUND"


def test_plain_404_without_404_html(tmp_path: Path):
    app = FastAPI()
    static_web.mount(app, build(tmp_path / "w", {"index.html": "I"}))
    r = TestClient(app).get("/nope")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


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
    assert r.status_code == 404
    found, _ = static_web.resolve(tmp_path / "web", "../secret.txt")
    assert found is None


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
    assert client.head("/nope").status_code == 404


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
        assert c.get("/family").text == "FAMILY"
        assert c.get("/healthz").status_code in (200, 503)
