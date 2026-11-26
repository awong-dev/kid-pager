"""Serves the web static export from the relay the way web/firebase.json does on Hosting."""

from __future__ import annotations

import atexit
import mimetypes
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

WEB_ROOT = Path("/srv/web")

# The web app is one client-routed page (web/app/page.tsx): any path that is not a file is the
# shell. Build assets are the exception, so a stale chunk URL fails instead of returning HTML.
SHELL = "index.html"
_ASSET_PREFIXES = ("_next/",)
_RELAY_PREFIXES = ("api/", "webhooks/", "bridge/", "fed/", "hooks/", "internal/")
_SW_FILES = ("sw.js", "firebase-messaging-sw.js")

# The home web build writes this literal wherever the base path belongs; `render` replaces every
# occurrence with `settings.base_path`. web/ must use exactly the same string.
BASE_PATH_PLACEHOLDER = "/__PAGER_BASE_PATH__"
_TEXT_SUFFIXES = (".html", ".js", ".css", ".webmanifest", ".json", ".txt")

mimetypes.add_type("application/manifest+json", ".webmanifest")


def _file(root: Path, rel: str) -> Path | None:
    if not rel or rel.endswith("/"):
        return None
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def resolve(root: Path, path: str) -> tuple[Path | None, int]:
    """`(file, status)` for a request path (no leading slash); `(None, 404)` if nothing matches."""
    root = root.resolve()
    rel = path.lstrip("/")
    found = (
        _file(root, rel)
        or _file(root, f"{rel}.html")
        or _file(root, f"{rel.rstrip('/')}/index.html" if rel else "index.html")
    )
    if found is not None:
        return found, 200
    if not rel.startswith(_ASSET_PREFIXES) and ".." not in rel.split("/"):
        shell = _file(root, SHELL)
        if shell is not None:
            return shell, 200
    return _file(root, "404.html"), 404


def render(root: Path, base_path: str) -> Path:
    """A copy of `root` with the placeholder replaced by `base_path`, in a fresh temp directory
    (removed at exit). Returns `root` itself when no text file contains the placeholder."""
    needle = BASE_PATH_PLACEHOLDER.encode()
    files = [p for p in sorted(root.rglob("*")) if p.is_file()]
    text = [p for p in files if p.suffix.lower() in _TEXT_SUFFIXES]
    if not any(needle in p.read_bytes() for p in text):
        return root
    out = Path(tempfile.mkdtemp(prefix="pager-web-"))
    atexit.register(shutil.rmtree, out, ignore_errors=True)
    for src in files:
        dst = out / src.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in _TEXT_SUFFIXES:
            dst.write_bytes(src.read_bytes().replace(needle, base_path.encode()))
        else:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
    return out


def mount(app: FastAPI, root: Path | None = None, base_path: str = "") -> None:
    root = root if root is not None else WEB_ROOT
    root = render(root, base_path)
    sw_headers = {"Service-Worker-Allowed": f"{base_path}/"}
    headers = {name: sw_headers for name in _SW_FILES}

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def static_web(path: str, request: Request) -> Response:
        if path.startswith(_RELAY_PREFIXES):
            raise HTTPException(status_code=404, detail="Not Found")
        found, status = resolve(root, path)
        if found is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(found, status_code=status, headers=headers.get(found.name))
