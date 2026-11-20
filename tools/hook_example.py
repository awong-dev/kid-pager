#!/usr/bin/env python3
"""Reference receiver for the relay's message hooks.

Verifies the X-KP-Signature, logs who wrote what to whom (the body is printed only with
--show-body), and answers `allow`; `--block-word X` blocks any message containing X.
Standard library only.

    python3 tools/hook_example.py --secret <secret shown once by the relay> --port 9000 \
        --block-word badword

The secret is unpadded base64url of 32 random bytes; the HMAC key is the decoded bytes.
The signature is sha256=hex(hmac_sha256(key, X-KP-Timestamp + "." + raw request body)).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_SKEW_S = 300


def verify(secret: str, headers, raw: bytes) -> bool:
    key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    ts = headers.get("X-KP-Timestamp", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > MAX_SKEW_S:
        return False
    want = (
        "sha256=" + hmac.new(key, ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(want, headers.get("X-KP-Signature", ""))


def decide(payload: dict, block_word: str | None) -> dict:
    body = payload["message"]["body"]
    if block_word and block_word.lower() in body.lower():
        return {
            "action": "block",
            "reason": f"contains {block_word!r}",
            "notifySender": "That message was not sent.",
            "labels": ["blocked-word"],
        }
    return {"action": "allow"}


def make_handler(secret: str, block_word: str | None, show_body: bool):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not verify(secret, self.headers, raw):
                self.send_response(401)
                self.end_headers()
                return
            payload = json.loads(raw)
            m, s = payload["message"], payload["sender"]
            print(
                f"{payload['mode']} {payload['id']} from @{s['alias']} to "
                f"{[r['alias'] for r in payload['recipients']]} ctx={len(payload['context'])}"
                + (f" body={m['body']!r}" if show_body else ""),
                flush=True,
            )
            out = json.dumps(
                decide(payload, block_word) if payload["mode"] == "filter" else {}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(
            self, *args
        ) -> None:  # the default logger prints the client address
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--secret", required=True)
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--block-word")
    ap.add_argument("--show-body", action="store_true")
    a = ap.parse_args()
    print(f"hook receiver on :{a.port}", flush=True)
    HTTPServer(
        ("", a.port), make_handler(a.secret, a.block_word, a.show_body)
    ).serve_forever()


if __name__ == "__main__":
    main()
