#!/usr/bin/env python3
"""Provisions EMQX's rule engine so device traffic on `pager/{id}/{up,status,loc}` and the
bootstrap ack `pager/boot/{bid}/up` reaches the relay's `POST /webhooks/mqtt`.

Uses only the standard library (`urllib`). Safe to re-run against the EMQX REST Management API.

Not provisioned here: the relay's REST-publish credential (`BROKER_API_KEY`/`BROKER_API_SECRET`),
which is seeded via `relay/emqx/bootstrap_api_keys.txt` and `EMQX_API_KEY__BOOTSTRAP_FILE`. The
`relay-1` MQTT credential created here gives the relay the mirror-image ACL of PROTOCOL.md §2 for
manual MQTT sessions. Per-device credentials and ACLs are pushed by `relay/app/emqx_admin.py`.

Usage (after `docker compose up -d` in `relay/`):
    python3 tools/emqx_setup.py
    python3 tools/emqx_setup.py --emqx-url http://localhost:18083 \\
        --relay-url http://relay:8000 --webhook-key dev-webhook-key \\
        --relay-mqtt-password dev-relay-mqtt-password

Safe to re-run: every resource is created if missing, updated in place if
already present (never errors on "already exists").
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

CONNECTOR_NAME = "relay_webhook"
ACTION_NAME = "relay_webhook_action"
RULE_ID = "pager_to_relay"
# `pager/boot/+/up` carries the bootstrap ack (`{v:1,ok:1}`, CBOR keys 0/29).
TOPICS = ["pager/+/up", "pager/+/status", "pager/+/loc", "pager/boot/+/up"]

# docs/PROTOCOL.md §2: the `relay` credential gets the mirror image of a device ACL.
# `-1` matches the MQTT client id reserved for the relay (§1).
RELAY_MQTT_USERNAME = "relay-1"


def _request(
    method: str, url: str, token: str | None = None, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            try:
                return resp.status, (json.loads(raw) if raw else None)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # /api/v5/status replies with a plain-text body, not JSON.
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else None)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, str(exc)


def wait_for_emqx(base_url: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        status, data = _request("GET", f"{base_url}/api/v5/status")
        if status == 200:
            return
        last = (status, data)
        time.sleep(2)
    raise SystemExit(f"EMQX at {base_url} never became ready (last: {last})")


def login(base_url: str, user: str, password: str) -> str:
    status, data = _request(
        "POST", f"{base_url}/api/v5/login", body={"username": user, "password": password}
    )
    if status != 200:
        raise SystemExit(f"EMQX login failed: {status} {data}")
    return data["token"]


def upsert_connector(base_url: str, token: str, relay_url: str) -> None:
    get_status, _ = _request(
        "GET", f"{base_url}/api/v5/connectors/http:{CONNECTOR_NAME}", token
    )
    if get_status == 200:
        # PUT rejects "type"/"name" -- they're fixed by the URL, not the body.
        status, data = _request(
            "PUT",
            f"{base_url}/api/v5/connectors/http:{CONNECTOR_NAME}",
            token,
            {"url": relay_url, "enable": True},
        )
    else:
        status, data = _request(
            "POST",
            f"{base_url}/api/v5/connectors",
            token,
            {"type": "http", "name": CONNECTOR_NAME, "url": relay_url, "enable": True},
        )
    if status not in (200, 201):
        raise SystemExit(f"failed to upsert EMQX connector: {status} {data}")


def upsert_action(base_url: str, token: str, webhook_key: str) -> None:
    parameters = {
        "path": "/webhooks/mqtt",
        "method": "post",
        "headers": {
            "X-Relay-Webhook-Key": webhook_key,
            "content-type": "application/json",
        },
        # "${.}" = the whole rule-engine event context as JSON. Resource option
        # `query_mode: sync` is required: EMQX 5.8.0 async mode left the first request inflight.
        "body": "${.}",
    }
    resource_opts = {"query_mode": "sync", "request_ttl": "10s"}
    get_status, _ = _request("GET", f"{base_url}/api/v5/actions/http:{ACTION_NAME}", token)
    if get_status == 200:
        status, data = _request(
            "PUT",
            f"{base_url}/api/v5/actions/http:{ACTION_NAME}",
            token,
            {
                "connector": CONNECTOR_NAME,
                "enable": True,
                "parameters": parameters,
                "resource_opts": resource_opts,
            },
        )
    else:
        status, data = _request(
            "POST",
            f"{base_url}/api/v5/actions",
            token,
            {
                "type": "http",
                "name": ACTION_NAME,
                "connector": CONNECTOR_NAME,
                "enable": True,
                "parameters": parameters,
                "resource_opts": resource_opts,
            },
        )
    if status not in (200, 201):
        raise SystemExit(f"failed to upsert EMQX action: {status} {data}")


def upsert_rule(base_url: str, token: str) -> None:
    topics_sql = ", ".join(f'"{t}"' for t in TOPICS)
    # `payload_b64` carries CBOR bytes intact (PROTOCOL.md §3.1): EMQX's JSON encoder replaces
    # every byte of a non-UTF-8 `payload` with U+FFFD. `payload` stays for plain-JSON envelopes.
    sql = (
        "SELECT topic, payload, base64_encode(payload) as payload_b64, qos, clientid "
        f"FROM {topics_sql}"
    )
    get_status, _ = _request("GET", f"{base_url}/api/v5/rules/{RULE_ID}", token)
    if get_status == 200:
        status, data = _request(
            "PUT",
            f"{base_url}/api/v5/rules/{RULE_ID}",
            token,
            {"sql": sql, "actions": [f"http:{ACTION_NAME}"], "enable": True},
        )
    else:
        status, data = _request(
            "POST",
            f"{base_url}/api/v5/rules",
            token,
            {"id": RULE_ID, "sql": sql, "actions": [f"http:{ACTION_NAME}"], "enable": True},
        )
    if status not in (200, 201):
        raise SystemExit(f"failed to upsert EMQX rule: {status} {data}")


def upsert_relay_user(base_url: str, token: str, username: str, password: str) -> None:
    """Creates or updates the `built_in_database` authentication user for the relay credential."""
    get_status, _ = _request(
        "GET",
        f"{base_url}/api/v5/authentication/password_based:built_in_database/users/{username}",
        token,
    )
    if get_status == 200:
        status, data = _request(
            "PUT",
            f"{base_url}/api/v5/authentication/password_based:built_in_database/users/{username}",
            token,
            {"password": password, "is_superuser": False},
        )
    else:
        status, data = _request(
            "POST",
            f"{base_url}/api/v5/authentication/password_based:built_in_database/users",
            token,
            {"user_id": username, "password": password, "is_superuser": False},
        )
    if status not in (200, 201):
        raise SystemExit(f"failed to upsert EMQX relay credential: {status} {data}")


def upsert_relay_acl(base_url: str, token: str, username: str) -> None:
    """Mirror image of a device's ACL (PROTOCOL.md §2); `PUT` replaces the whole rule list."""
    rules = [
        {"topic": "pager/+/up", "permission": "allow", "action": "subscribe"},
        {"topic": "pager/+/status", "permission": "allow", "action": "subscribe"},
        {"topic": "pager/+/loc", "permission": "allow", "action": "subscribe"},
        {"topic": "pager/+/down", "permission": "allow", "action": "publish"},
    ]
    status, data = _request(
        "PUT",
        f"{base_url}/api/v5/authorization/sources/built_in_database/rules/users/{username}",
        token,
        {"username": username, "rules": rules},
    )
    if status not in (200, 204):
        raise SystemExit(f"failed to upsert EMQX relay ACL: {status} {data}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--emqx-url",
        default="http://localhost:18083",
        help="EMQX dashboard/management API base URL, as reachable from wherever this script runs",
    )
    parser.add_argument(
        "--relay-url",
        default="http://relay:8000",
        help="relay base URL as reachable from *inside* the EMQX container (compose service name)",
    )
    parser.add_argument("--webhook-key", default="dev-webhook-key")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="public")
    parser.add_argument(
        "--relay-mqtt-password",
        default="dev-relay-mqtt-password",
        help=(
            f"password for the {RELAY_MQTT_USERNAME!r} built_in_database MQTT "
            "credential (docs/PROTOCOL.md §2's 'relay credential', mirror-image "
            "ACL -- see this script's module docstring for who, if anyone, uses it)"
        ),
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    wait_for_emqx(args.emqx_url)
    token = login(args.emqx_url, args.admin_user, args.admin_password)
    upsert_connector(args.emqx_url, token, args.relay_url)
    upsert_action(args.emqx_url, token, args.webhook_key)
    upsert_rule(args.emqx_url, token)
    upsert_relay_user(args.emqx_url, token, RELAY_MQTT_USERNAME, args.relay_mqtt_password)
    upsert_relay_acl(args.emqx_url, token, RELAY_MQTT_USERNAME)
    print(
        f"EMQX rule engine configured: {', '.join(TOPICS)} -> "
        f"{args.relay_url}/webhooks/mqtt"
    )
    print(f"EMQX '{RELAY_MQTT_USERNAME}' credential + mirror-image ACL provisioned")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        print(f"emqx_setup.py failed: {exc}", file=sys.stderr)
        raise
