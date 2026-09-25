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
from app.store import cas as cas_store
from app.store import cells as cells_store
from app.store import conversations as conversations_store
from app.store import device_secrets as device_secrets_store
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


def test_device_secrets_is_default_deny(two_pairs):
    """`deviceSecrets/{d}` (HMAC key, MQTT password hash) is unreadable by the device's owner and by
    an admin client."""
    devices_store.create_device(
        device_id="pgr-secret-rules-1",
        owner_uid="u1",
        label="d",
        mqtt_username="pgr-secret-rules-1",
        mqtt_password_hash="x",
    )
    device_secrets_store.create(
        "pgr-secret-rules-1", hmac_key=b"k" * 32, mqtt_password_hash="hash"
    )

    owner_token = mint_id_token("u1")
    resp = _get("deviceSecrets/pgr-secret-rules-1", owner_token)
    assert resp.status_code == 403

    fb_auth.create_user(uid="admin1", email="admin1@example.com")
    users_store.create_user(uid="admin1", alias="admin1", display_name="Admin", role="admin")
    fb_auth.set_custom_user_claims("admin1", {"admin": True})
    admin_token = mint_id_token("admin1")
    resp_admin = _get("deviceSecrets/pgr-secret-rules-1", admin_token)
    assert resp_admin.status_code == 403

    resp_unauth = _get("deviceSecrets/pgr-secret-rules-1", None)
    assert resp_unauth.status_code == 403

    resp_write = _write(
        "deviceSecrets/pgr-secret-rules-1", owner_token, {"mqttPasswordHash": "hacked"}
    )
    assert resp_write.status_code == 403


def test_cas_is_default_deny(two_pairs):
    """`cas/{sha256hex}` has no client read path (the public read is `GET /ca/{sha256hex}.pem`):
    unreadable by a registered user and by an admin."""
    sha_hex = "ab" * 32
    cas_store.remember(sha_hex, "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

    owner_token = mint_id_token("u1")
    resp = _get(f"cas/{sha_hex}", owner_token)
    assert resp.status_code == 403

    fb_auth.create_user(uid="admin-cas", email="admin-cas@example.com")
    users_store.create_user(uid="admin-cas", alias="admincas", display_name="Admin", role="admin")
    fb_auth.set_custom_user_claims("admin-cas", {"admin": True})
    admin_token = mint_id_token("admin-cas")
    resp_admin = _get(f"cas/{sha_hex}", admin_token)
    assert resp_admin.status_code == 403

    resp_unauth = _get(f"cas/{sha_hex}", None)
    assert resp_unauth.status_code == 403

    resp_write = _write(f"cas/{sha_hex}", owner_token, {"pem": "hacked"})
    assert resp_write.status_code == 403


def test_cells_is_default_deny(two_pairs):
    """`cells/{mcc}-{mnc}-{tac}-{ci}` (the cell geolocation cache, PROTOCOL.md §13.2) has no match
    block: unreadable by a registered user and by an admin."""
    cells_store.remember_resolved(
        "310", "410", 12345, 87654321, lat=1.0, lon=2.0, acc_m=1000, provider="google"
    )
    key = cells_store.cache_key("310", "410", 12345, 87654321)

    owner_token = mint_id_token("u1")
    resp = _get(f"cells/{key}", owner_token)
    assert resp.status_code == 403

    fb_auth.create_user(uid="admin-cells", email="admin-cells@example.com")
    users_store.create_user(uid="admin-cells", alias="admincells", display_name="Admin", role="admin")
    fb_auth.set_custom_user_claims("admin-cells", {"admin": True})
    admin_token = mint_id_token("admin-cells")
    resp_admin = _get(f"cells/{key}", admin_token)
    assert resp_admin.status_code == 403

    resp_unauth = _get(f"cells/{key}", None)
    assert resp_unauth.status_code == 403

    resp_write = _write(f"cells/{key}", owner_token, {"lat": 999.0})
    assert resp_write.status_code == 403


    """`contactRequests/{deviceId}_{reqId}` is readable by the device owner and a family admin of
    from app.store import contacts as contacts_store

    devices_store.create_device(
        device_id="pgr-rules-contacts-1",
        owner_uid="u1",
        label="d",
        mqtt_username="pgr-rules-contacts-1",
        mqtt_password_hash="x",
    )
    )
    doc_key = contacts_store.key("pgr-rules-contacts-1", "u_rules1")

    owner_token = mint_id_token("u1")
    resp_owner = _get(f"contactRequests/{doc_key}", owner_token)
    assert resp_owner.status_code == 200

    other_token = mint_id_token("u3")
    resp_other = _get(f"contactRequests/{doc_key}", other_token)
    assert resp_other.status_code == 403

    fb_auth.create_user(uid="admin-contacts-1", email="admin-contacts-1@example.com")
    users_store.create_user(
        uid="admin-contacts-1", alias="admincontacts1", display_name="Admin", role="admin"
    )
    admin_token = mint_id_token("admin-contacts-1")
    resp_admin = _get(f"contactRequests/{doc_key}", admin_token)
    assert resp_admin.status_code == 200

    resp_unauth = _get(f"contactRequests/{doc_key}", None)
    assert resp_unauth.status_code == 403


    """`devices/{id}/smsLog/{logId}` is readable by the owner and the device's family admin only; a
    from app.store import sms as sms_store

    devices_store.create_device(
        device_id="pgr-rules-sms-1",
        owner_uid="u2",
        label="d",
        mqtt_username="pgr-rules-sms-1",
        mqtt_password_hash="x",
    )
    devices_store.set_locatable_by("pgr-rules-sms-1", ["u1"])
    sms_store.create_log(
        "pgr-rules-sms-1",
        "s_rules1",
        ts=1000,
        sms_ts=1000,
        dir_="out",
        peer="+15550001111",
        st="sent",
        body="hi",
    )

    owner_token = mint_id_token("u2")
    resp_owner = _get("devices/pgr-rules-sms-1/smsLog/s_rules1", owner_token)
    assert resp_owner.status_code == 200

    # A `locate`-permission uid is neither owner nor admin: denied, though it can read the device's
    # `locations`.
    locate_token = mint_id_token("u1")
    resp_locate = _get("devices/pgr-rules-sms-1/smsLog/s_rules1", locate_token)
    assert resp_locate.status_code == 403

    other_token = mint_id_token("u3")
    resp_other = _get("devices/pgr-rules-sms-1/smsLog/s_rules1", other_token)
    assert resp_other.status_code == 403

    resp_unauth = _get("devices/pgr-rules-sms-1/smsLog/s_rules1", None)
    assert resp_unauth.status_code == 403

    fb_auth.create_user(uid="admin-sms-1", email="admin-sms-1@example.com")
    users_store.create_user(uid="admin-sms-1", alias="adminsms1", display_name="Admin", role="admin")
    admin_token = mint_id_token("admin-sms-1")
    resp_admin = _get("devices/pgr-rules-sms-1/smsLog/s_rules1", admin_token)
    assert resp_admin.status_code == 200

    resp_write = _write(
        "devices/pgr-rules-sms-1/smsLog/s_rules1", owner_token, {"body": "hacked"}
    )
    assert resp_write.status_code == 403


def test_sms_contacts_field_not_client_writable(two_pairs):
    """`devices/{id}.smsContacts` is a field of the readable device document; every client write is
    denied by the blanket `allow write: if false`."""
    devices_store.create_device(
        device_id="pgr-rules-sms-2",
        owner_uid="u2",
        label="d",
        mqtt_username="pgr-rules-sms-2",
        mqtt_password_hash="x",
    )
    owner_token = mint_id_token("u2")
    resp_write = _write(
        "devices/pgr-rules-sms-2",
        owner_token,
        {"smsContacts": "hacked"},
    )
    assert resp_write.status_code == 403


@pytest.fixture
def group_convo():
    """A 3-member group g1/g2/g3 (g4 is an outsider) with one group message from g1: two copies,
    `g1->g2` and `g1->g3`, sharing one `groupMsgId`/`seq` and the group's `convKey`."""
    for uid, email, alias in (
        ("g1", "g1@example.com", "galice"),
        ("g2", "g2@example.com", "gbob"),
        ("g3", "g3@example.com", "gcarol"),
        ("g4", "g4@example.com", "goutsider"),
    ):
        fb_auth.create_user(uid=uid, email=email)
        users_store.create_user(uid=uid, alias=alias, display_name=alias)

    conv = conversations_store.create_group(
        name="Family", alias="rules-fam", member_uids=["g1", "g2", "g3"], created_by="g1"
    )
    seq = messages_store.allocate_seq()
    group_msg_id = "gm_rules1"
    copy_to_g2 = messages_store.create_message(
        sender_uid="g1",
        recipient_uid="g2",
        kind="text",
        ts=1000,
        body="hi",
        conv_key=conv.convKey,
        uids=sorted(["g1", "g2"]),
        seq=seq,
        group_msg_id=group_msg_id,
        sender_alias="galice",
    )
    copy_to_g3 = messages_store.create_message(
        sender_uid="g1",
        recipient_uid="g3",
        kind="text",
        ts=1000,
        body="hi",
        conv_key=conv.convKey,
        uids=sorted(["g1", "g3"]),
        seq=seq,
        group_msg_id=group_msg_id,
        sender_alias="galice",
    )
    return conv, copy_to_g2, copy_to_g3


def test_group_member_can_read_conversation_and_own_copy(group_convo):
    conv, copy_to_g2, _copy_to_g3 = group_convo
    token = mint_id_token("g2")
    resp_conv = _get(f"conversations/{conv.convKey}", token)
    assert resp_conv.status_code == 200
    resp_msg = _get(f"messages/{copy_to_g2.id}", token)
    assert resp_msg.status_code == 200


def test_group_non_member_cannot_read_conversation_or_any_copy(group_convo):
    conv, copy_to_g2, copy_to_g3 = group_convo
    token = mint_id_token("g4")
    assert _get(f"conversations/{conv.convKey}", token).status_code == 403
    assert _get(f"messages/{copy_to_g2.id}", token).status_code == 403
    assert _get(f"messages/{copy_to_g3.id}", token).status_code == 403


def test_group_member_cannot_read_a_copy_addressed_to_a_different_member(group_convo):
    """A group copy's `uids` is the sender/recipient pair, so a member not party to a copy cannot
    read it, though they read the group's `conversations/{k}` summary."""
    _conv, _copy_to_g2, copy_to_g3 = group_convo
    token = mint_id_token("g2")
    resp = _get(f"messages/{copy_to_g3.id}", token)
    assert resp.status_code == 403


def test_group_thread_convkey_only_query_is_denied(group_convo):
    conv, _copy_to_g2, _copy_to_g3 = group_convo
    body = {
        "structuredQuery": {
            "from": [{"collectionId": "messages"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": "convKey"},
                    "op": "EQUAL",
                    "value": {"stringValue": conv.convKey},
                }
            },
        }
    }
    resp = _run_query("", mint_id_token("g1"), body)
    assert resp.status_code == 403, resp.text


def test_group_sender_can_list_query_both_of_their_own_copies(group_convo):
    """The sender is in every copy's pair, so the `convKey` + `uids array-contains <self>` query
    returns both copies to the sender."""
    conv, _copy_to_g2, _copy_to_g3 = group_convo
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
                                "value": {"stringValue": conv.convKey},
                            }
                        },
                        {
                            "fieldFilter": {
                                "field": {"fieldPath": "uids"},
                                "op": "ARRAY_CONTAINS",
                                "value": {"stringValue": "g1"},
                            }
                        },
                    ],
                }
            },
        }
    }
    resp = _run_query("", mint_id_token("g1"), body)
    assert resp.status_code == 200, resp.text
    docs = [entry for entry in resp.json() if "document" in entry]
    assert len(docs) == 2


def test_former_group_member_keeps_old_copies_but_loses_the_summary(group_convo):
    """A former member keeps read access to copies they were party to and loses the conversation
    summary."""
    conv, copy_to_g2, _copy_to_g3 = group_convo
    conversations_store.remove_member(conv.convKey, "g2")
    token = mint_id_token("g2")

    resp_msg = _get(f"messages/{copy_to_g2.id}", token)
    assert resp_msg.status_code == 200

    resp_conv = _get(f"conversations/{conv.convKey}", token)
    assert resp_conv.status_code == 403
