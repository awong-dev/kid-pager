"""`relay/firestore.rules` tested through the Firestore emulator's REST API with real ID tokens,
since firebase-admin (what the relay uses) bypasses rules."""

from __future__ import annotations

import os
import time

import httpx
import pytest
from firebase_admin import auth as fb_auth

from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.store import users as users_store

FIRESTORE_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "localhost:8080")
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "demo-pager")
BASE_URL = f"http://{FIRESTORE_HOST}/v1/projects/{PROJECT_ID}/databases/(default)/documents"


def _get(path: str, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.get(f"{BASE_URL}/{path}", headers=headers, timeout=10.0)


def _write(path: str, token: str | None, fields: dict) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # Firestore REST's document body shape: {"fields": {name: {stringValue: ...}}}.
    body = {"fields": {k: {"stringValue": str(v)} for k, v in fields.items()}}
    return httpx.patch(f"{BASE_URL}/{path}", headers=headers, json=body, timeout=10.0)


def _run_query(parent_path: str, token: str | None, body: dict) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    base = f"{BASE_URL}/{parent_path}" if parent_path else BASE_URL
    return httpx.post(f"{base}:runQuery", headers=headers, json=body, timeout=10.0)


@pytest.fixture
def two_pairs():
    """u1<->u2 have a message/conversation; u3 is unrelated to both."""
    for uid, email, alias in (
        ("u1", "u1@example.com", "alice"),
        ("u2", "u2@example.com", "bob"),
        ("u3", "u3@example.com", "carol"),
    ):
        fb_auth.create_user(uid=uid, email=email)
        users_store.create_user(uid=uid, alias=alias, display_name=alias)

    msg = messages_store.create_message(
        sender_uid="u1", recipient_uid="u2", kind="text", ts=1000, body="hello"
    )
    return msg


def test_party_can_read_their_own_message(two_pairs):
    msg = two_pairs
    token = mint_id_token("u1")
    resp = _get(f"messages/{msg.id}", token)
    assert resp.status_code == 200


def test_non_party_cannot_read_the_message(two_pairs):
    msg = two_pairs
    token = mint_id_token("u3")
    resp = _get(f"messages/{msg.id}", token)
    assert resp.status_code == 403


def test_unauthenticated_cannot_read_the_message(two_pairs):
    msg = two_pairs
    resp = _get(f"messages/{msg.id}", None)
    assert resp.status_code == 403


def test_party_can_list_query_their_thread(two_pairs):
    """The thread listener query: `convKey` plus `uids array-contains <self>`; a `list` is checked
    against the query's declared filters before any document is read."""
    key = messages_store.conv_key("u1", "u2")
    body = {
        "structuredQuery": {
            "from": [{"collectionId": "messages"}],
            "where": {
                "compositeFilter": {
                    "op": "AND",
                    "filters": [
                        {
                            "fieldFilter": {
                                "field": {"fieldPath": "convKey"},
                                "op": "EQUAL",
                                "value": {"stringValue": key},
                            }
                        },
                        {
                            "fieldFilter": {
                                "field": {"fieldPath": "uids"},
                                "op": "ARRAY_CONTAINS",
                                "value": {"stringValue": "u1"},
                            }
                        },
                    ],
                }
            },
            "orderBy": [{"field": {"fieldPath": "seq"}, "direction": "DESCENDING"}],
            "limit": 50,
        }
    }
    resp = _run_query("", mint_id_token("u1"), body)
    assert resp.status_code == 200, resp.text
    assert any("document" in entry for entry in resp.json()), resp.text


def test_thread_list_query_without_the_uids_filter_is_denied(two_pairs):
    """The same query without `uids array-contains` is denied even for a party to the
    conversation."""
    key = messages_store.conv_key("u1", "u2")
    body = {
        "structuredQuery": {
            "from": [{"collectionId": "messages"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": "convKey"},
                    "op": "EQUAL",
                    "value": {"stringValue": key},
                }
            },
        }
    }
    resp = _run_query("", mint_id_token("u1"), body)
    assert resp.status_code == 403, resp.text


def test_non_party_cannot_list_query_a_thread(two_pairs):
    """A `uids array-contains u3` filter is provable but matches nothing of u1<->u2's: an outsider
    gets an empty result."""
    body = {
        "structuredQuery": {
            "from": [{"collectionId": "messages"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": "uids"},
                    "op": "ARRAY_CONTAINS",
                    "value": {"stringValue": "u3"},
                }
            },
        }
    }
    resp = _run_query("", mint_id_token("u3"), body)
    assert resp.status_code == 200, resp.text
    assert not any("document" in entry for entry in resp.json()), resp.text


def test_party_can_read_their_conversation(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    token = mint_id_token("u2")
    resp = _get(f"conversations/{key}", token)
    assert resp.status_code == 200


def test_non_party_cannot_read_an_unrelated_conversation(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    token = mint_id_token("u3")
    resp = _get(f"conversations/{key}", token)
    assert resp.status_code == 403


def test_self_can_read_own_user_doc(two_pairs):
    token = mint_id_token("u1")
    resp = _get("users/u1", token)
    assert resp.status_code == 200


def test_other_user_cannot_read_someone_elses_user_doc(two_pairs):
    token = mint_id_token("u3")
    resp = _get("users/u1", token)
    assert resp.status_code == 403


def test_unregistered_signed_in_uid_cannot_read_settings(two_pairs):
    # A real Firebase Auth account with no users/{uid} doc: registered() must fail, as
    # app.auth.require_user does.
    fb_auth.create_user(uid="stranger", email="stranger@example.com")
    token = mint_id_token("stranger")
    resp = _get("settings/retention", token)
    assert resp.status_code == 403


def test_registered_user_can_read_settings(two_pairs):
    from app.store import settings as settings_store

    settings_store.set_retention(
        messages=settings_store.RetentionSetting(n=4, unit="weeks"),
        locations=settings_store.RetentionSetting(n=1, unit="weeks"),
    )
    token = mint_id_token("u1")
    resp = _get("settings/retention", token)
    assert resp.status_code == 200


def test_device_owner_can_read_their_device(two_pairs):
    devices_store.create_device(
        device_id="pgr-rules-1",
        owner_uid="u2",
        label="d",
        mqtt_username="pgr-rules-1",
        mqtt_password_hash="x",
    )
    token = mint_id_token("u2")
    resp = _get("devices/pgr-rules-1", token)
    assert resp.status_code == 200


def test_non_owner_non_locator_cannot_read_device(two_pairs):
    devices_store.create_device(
        device_id="pgr-rules-2",
        owner_uid="u2",
        label="d",
        mqtt_username="pgr-rules-2",
        mqtt_password_hash="x",
    )
    token = mint_id_token("u3")
    resp = _get("devices/pgr-rules-2", token)
    assert resp.status_code == 403


def test_locatable_by_uid_can_read_device_and_its_locations(two_pairs):
    devices_store.create_device(
        device_id="pgr-rules-3",
        owner_uid="u2",
        label="d",
        mqtt_username="pgr-rules-3",
        mqtt_password_hash="x",
    )
    devices_store.set_locatable_by("pgr-rules-3", ["u1"])

    token = mint_id_token("u1")
    resp = _get("devices/pgr-rules-3", token)
    assert resp.status_code == 200

    from app.store.locations import LocationFix, add_location

    loc_id = add_location(
        "pgr-rules-3", LocationFix(ts=1, fixTs=1, lat=1.0, lon=2.0)
    )
    resp2 = _get(f"devices/pgr-rules-3/locations/{loc_id}", token)
    assert resp2.status_code == 200

    other_token = mint_id_token("u3")
    resp3 = _get(f"devices/pgr-rules-3/locations/{loc_id}", other_token)
    assert resp3.status_code == 403


def test_locatable_by_uid_can_list_query_devices_and_their_locations(two_pairs):
    """A `list` is checked against the query's declared filters alone: `locatableBy array_contains
    <caller>` is provable, `ownerUid` alone is not."""
    devices_store.create_device(
        device_id="pgr-rules-4",
        owner_uid="u2",
        label="d",
        mqtt_username="pgr-rules-4",
        mqtt_password_hash="x",
    )
    devices_store.set_locatable_by("pgr-rules-4", ["u1"])
    from app.store.locations import LocationFix, add_location

    add_location("pgr-rules-4", LocationFix(ts=1, fixTs=1, lat=1.0, lon=2.0))

    token = mint_id_token("u1")
    devices_query = {
        "structuredQuery": {
            "from": [{"collectionId": "devices"}],
            "where": {
                }
            },
        }
    }
    resp = _run_query("", token, devices_query)
    assert resp.status_code == 200, resp.text
    names = [row["document"]["name"] for row in resp.json() if "document" in row]
    assert any(n.endswith("/devices/pgr-rules-4") for n in names)

    locations_query = {
        "structuredQuery": {
            "from": [{"collectionId": "locations"}],
            "orderBy": [{"field": {"fieldPath": "createdAt"}, "direction": "DESCENDING"}],
        }
    }
    resp2 = _run_query("devices/pgr-rules-4", token, locations_query)
    assert resp2.status_code == 200, resp2.text
    assert len([row for row in resp2.json() if "document" in row]) == 1

    other_token = mint_id_token("u3")
    other_query = {
        "structuredQuery": {
            "from": [{"collectionId": "devices"}],
            "where": {
                }
            },
        }
    }
    resp3 = _run_query("", other_token, other_query)
    assert resp3.status_code == 200, resp3.text
    assert [row for row in resp3.json() if "document" in row] == []


def test_allow_edge_readable_by_either_party_not_a_third_party(two_pairs):
    allow_store.set_edge("u1", "u2", message=True, locate=True)
    token_party = mint_id_token("u2")
    resp = _get("allow/u1_u2", token_party)
    assert resp.status_code == 200

    token_other = mint_id_token("u3")
    resp2 = _get("allow/u1_u2", token_other)
    assert resp2.status_code == 403


def test_clients_cannot_write_anything(two_pairs):
    token = mint_id_token("u1")
    resp = _write("users/u1", token, {"displayName": "hacked"})
    assert resp.status_code == 403


def test_gchat_spaces_is_default_deny(two_pairs):
    backends_store.set_gchat_space("SPACERULES1", "u1", "bid1", "users/111")

    owner_token = mint_id_token("u1")
    resp = _get("gchatSpaces/SPACERULES1", owner_token)
    assert resp.status_code == 403

    resp_unauth = _get("gchatSpaces/SPACERULES1", None)
    assert resp_unauth.status_code == 403

    resp_write = _write("gchatSpaces/SPACERULES1", owner_token, {"uid": "u3"})
    assert resp_write.status_code == 403


def test_gchat_link_codes_is_default_deny(two_pairs):
    backends_store.set_gchat_link_code("999111", "u1", "bid1", int(time.time()) + 600)

    owner_token = mint_id_token("u1")
    resp = _get("gchatLinkCodes/999111", owner_token)
    assert resp.status_code == 403

    resp_unauth = _get("gchatLinkCodes/999111", None)
    assert resp_unauth.status_code == 403

    resp_write = _write("gchatLinkCodes/999111", owner_token, {"uid": "u3"})
    assert resp_write.status_code == 403


