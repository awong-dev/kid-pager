"""`app/readauth.py` (the Python port of `firestore.rules`) against every case of
`tests/test_rules.py`, with the same fixtures and expected allow/deny."""

from __future__ import annotations

import time

import pytest

from app import live, readauth
from app.auth import Principal
from app.db.firestore import get_db
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import cas as cas_store
from app.store import cells as cells_store
from app.store import conversations as conversations_store
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.store import users as users_store
from tests.firebase_test_utils import seed_legacy_contact_request


def P(uid: str, role: str | None = None, fam: str | None = None) -> Principal:
    return Principal(uid=uid, role=role or "member", family_id=fam)


def _get(path: str, p: Principal | None) -> bool:
    parent, _, doc_id = path.rpartition("/")
    snap = get_db().collection(parent).document(doc_id).get()
    return readauth.can_read(p, path, snap.to_dict() if snap.exists else None)


def _write(path: str, p: Principal | None, fields: dict) -> bool:
    return readauth.can_write(p, path)


def _run_query(p: Principal | None, q: dict) -> list[str] | None:
    """`None` = refused (403); else the readable result paths (200)."""
    if not readauth.query_admissible(p, q):
        return None
    return [path for path, data in live.run_query(q) if readauth.can_read(p, path, data)]


def _set_family(collection: str, doc_id: str, family_id: str | None) -> None:
    get_db().collection(collection).document(doc_id).set({"familyId": family_id}, merge=True)


def _set_allow_family_ids(from_uid: str, to_uid: str, family_ids: list[str]) -> None:
    get_db().collection("allow").document(f"{from_uid}_{to_uid}").set(
        {"familyIds": family_ids}, merge=True
    )


def _set_family_ids(collection: str, doc_id: str, family_ids: list[str]) -> None:
    get_db().collection(collection).document(doc_id).set({"familyIds": family_ids}, merge=True)


def _family_ids_query(collection: str, family_id: str) -> dict:
    return {"collection": collection, "where": [["familyIds", "array-contains", family_id]]}


def _make_family_admin(uid: str, alias: str, fam: str) -> Principal:
    users_store.create_user(uid=uid, alias=alias, display_name="Admin", role="admin")
    return P(uid, "admin", fam)


def _create_family(family_id: str, name: str = "Fam") -> None:
    get_db().collection("families").document(family_id).set(
        {"name": name, "blockedNumbers": [], "createdBy": "system"}
    )


@pytest.fixture
def two_pairs():
    """u1<->u2 have a message/conversation; u3 is unrelated to both."""
    for uid, alias in (("u1", "alice"), ("u2", "bob"), ("u3", "carol")):
        users_store.create_user(uid=uid, alias=alias, display_name=alias)
    return messages_store.create_message(
        sender_uid="u1", recipient_uid="u2", kind="text", ts=1000, body="hello"
    )


def test_party_can_read_their_own_message(two_pairs):
    assert _get(f"messages/{two_pairs.id}", P("u1"))


def test_non_party_cannot_read_the_message(two_pairs):
    assert not _get(f"messages/{two_pairs.id}", P("u3"))


def test_unauthenticated_cannot_read_the_message(two_pairs):
    assert not _get(f"messages/{two_pairs.id}", None)


def test_party_can_list_query_their_thread(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    q = {
        "collection": "messages",
        "where": [["convKey", "==", key], ["uids", "array-contains", "u1"]],
        "orderBy": [["seq", "desc"]],
        "limit": 50,
    }
    rows = _run_query(P("u1"), q)
    assert rows is not None
    assert rows


def test_thread_list_query_without_the_uids_filter_is_denied(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    q = {"collection": "messages", "where": [["convKey", "==", key]]}
    assert _run_query(P("u1"), q) is None


def test_non_party_cannot_list_query_a_thread(two_pairs):
    q = {"collection": "messages", "where": [["uids", "array-contains", "u3"]]}
    assert _run_query(P("u3"), q) == []


def test_party_can_read_their_conversation(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    assert _get(f"conversations/{key}", P("u2"))


def test_non_party_cannot_read_an_unrelated_conversation(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    assert not _get(f"conversations/{key}", P("u3"))


def test_self_can_read_own_user_doc(two_pairs):
    assert _get("users/u1", P("u1"))


def test_other_user_cannot_read_someone_elses_user_doc(two_pairs):
    assert not _get("users/u1", P("u3"))


def test_unregistered_signed_in_uid_cannot_read_settings(two_pairs):
    assert not _get("settings/retention", P("stranger"))


def test_registered_user_can_read_settings(two_pairs):
    from app.store import settings as settings_store

    settings_store.set_retention(
        messages=settings_store.RetentionSetting(n=4, unit="weeks"),
        locations=settings_store.RetentionSetting(n=1, unit="weeks"),
    )
    assert _get("settings/retention", P("u1"))


def _device(device_id: str, owner_uid: str) -> None:
    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner_uid,
        label="d",
        mqtt_username=device_id,
        mqtt_password_hash="x",
    )


def test_device_owner_can_read_their_device(two_pairs):
    _device("pgr-rules-1", "u2")
    _set_family("devices", "pgr-rules-1", "famA")
    assert _get("devices/pgr-rules-1", P("u2", fam="famA"))


def test_non_owner_non_locator_cannot_read_device(two_pairs):
    _device("pgr-rules-2", "u2")
    assert not _get("devices/pgr-rules-2", P("u3"))


def test_locatable_by_uid_can_read_device_and_its_locations(two_pairs):
    from app.store.locations import LocationFix, add_location

    _device("pgr-rules-3", "u2")
    devices_store.set_locatable_by("pgr-rules-3", ["u1"])
    _set_family("devices", "pgr-rules-3", "famA")
    u1 = P("u1", fam="famA")
    assert _get("devices/pgr-rules-3", u1)

    loc_id = add_location("pgr-rules-3", LocationFix(ts=1, fixTs=1, lat=1.0, lon=2.0))
    assert _get(f"devices/pgr-rules-3/locations/{loc_id}", u1)
    assert not _get(f"devices/pgr-rules-3/locations/{loc_id}", P("u3"))


def test_owner_can_read_own_device_locations_but_not_non_owner_or_admin(two_pairs):
    from app.store.locations import LocationFix, add_location

    _device("pgr-rules-own-1", "u2")
    _set_family("devices", "pgr-rules-own-1", "famA")
    loc_id = add_location("pgr-rules-own-1", LocationFix(ts=1, fixTs=1, lat=1.0, lon=2.0))
    path = f"devices/pgr-rules-own-1/locations/{loc_id}"
    list_query = {
        "collection": "devices/pgr-rules-own-1/locations",
        "orderBy": [["createdAt", "desc"]],
    }

    owner = P("u2", fam="famA")
    assert _get(path, owner)
    rows = _run_query(owner, list_query)
    assert rows is not None and len(rows) == 1

    assert not _get(path, P("u3"))
    assert _run_query(P("u3"), list_query) is None

    users_store.create_user(uid="admin-loc-1", alias="adminloc1", display_name="Admin", role="admin")
    bare_admin = P("admin-loc-1")  # `{"admin": True}` claim only
    assert not _get(path, bare_admin)
    assert _run_query(bare_admin, list_query) is None

    assert not _get(path, None)


def test_locations_are_client_read_only(two_pairs):
    _device("pgr-rules-3b", "u1")
    assert not _write("devices/pgr-rules-3b/locations/fake1", P("u1"), {"lat": 1.0, "lon": 2.0})


def test_locatable_by_uid_can_list_query_devices_and_their_locations(two_pairs):
    from app.store.locations import LocationFix, add_location

    _device("pgr-rules-4", "u2")
    devices_store.set_locatable_by("pgr-rules-4", ["u1"])
    _set_family("devices", "pgr-rules-4", "famA")
    add_location("pgr-rules-4", LocationFix(ts=1, fixTs=1, lat=1.0, lon=2.0))

    u1 = P("u1", fam="famA")
    devices_query = {
        "collection": "devices",
        "where": [["locatableBy", "array-contains", "u1"], ["familyId", "==", "famA"]],
    }
    rows = _run_query(u1, devices_query)
    assert rows is not None
    assert "devices/pgr-rules-4" in rows

    locations_query = {
        "collection": "devices/pgr-rules-4/locations",
        "orderBy": [["createdAt", "desc"]],
    }
    rows2 = _run_query(u1, locations_query)
    assert rows2 is not None and len(rows2) == 1

    other_query = {
        "collection": "devices",
        "where": [["locatableBy", "array-contains", "u3"], ["familyId", "==", "famA"]],
    }
    assert _run_query(P("u3", fam="famA"), other_query) == []


def test_allow_edge_readable_by_either_party_not_a_third_party(two_pairs):
    allow_store.set_edge("u1", "u2", message=True, locate=True)
    assert _get("allow/u1_u2", P("u2"))
    assert not _get("allow/u1_u2", P("u3"))


def test_clients_cannot_write_anything(two_pairs):
    assert not _write("users/u1", P("u1"), {"displayName": "hacked"})


def test_gchat_spaces_is_default_deny(two_pairs):
    backends_store.set_gchat_space("SPACERULES1", "u1", "bid1", "users/111")
    assert not _get("gchatSpaces/SPACERULES1", P("u1"))
    assert not _get("gchatSpaces/SPACERULES1", None)
    assert not _write("gchatSpaces/SPACERULES1", P("u1"), {"uid": "u3"})


def test_gchat_link_codes_is_default_deny(two_pairs):
    backends_store.set_gchat_link_code("999111", "u1", "bid1", int(time.time()) + 600)
    assert not _get("gchatLinkCodes/999111", P("u1"))
    assert not _get("gchatLinkCodes/999111", None)
    assert not _write("gchatLinkCodes/999111", P("u1"), {"uid": "u3"})


def test_contact_names_is_default_deny(two_pairs):
    from app.store import externals as externals_store

    ext = externals_store.get_or_create("fam-rules", "+15550001111", "Grandma")
    doc_id = externals_store._name_ref("fam-rules", externals_store.name_key("Grandma")).id
    assert not _get(f"contactNames/{doc_id}", P("u1"))
    assert ext.uid
    assert not _get(f"contactNames/{doc_id}", None)
    assert not _write(f"contactNames/{doc_id}", P("u1"), {"uid": "hacked"})


def test_sms_numbers_and_held_sms_are_default_deny(two_pairs):
    from app.store import held_sms as held_sms_store

    users_store.set_sms_number("u1", "+15550007777")
    held_sms_store.create(
        "SMrules1", family_id="fam-rules", to_uid="u1", from_phone="+15550008888", body="secret"
    )
    for path in ("smsNumbers/+15550007777", "heldSms/SMrules1"):
        assert not _get(path, P("u1"))
        assert not _get(path, None)
        assert not _write(path, P("u1"), {"uid": "hacked"})


def test_bridge_collections_are_default_deny(two_pairs):
    from app.store import bridge_conversations as conv_store
    from app.store import bridge_outbox
    from app.store import bridges as bridges_store
    from app.store import held_chat as held_chat_store

    _create_family("fam-bridge")
    admin = _make_family_admin("bridge-admin", "badmin", "fam-bridge")
    other_admin = _make_family_admin("bridge-admin2", "badmin2", "fam-other")
    member = P("u1")
    bridge = bridges_store.create("u1", "fam-bridge", "phone", "bridge-admin")
    item = bridge_outbox.enqueue_send(bridge, "m_1", "sms", source="sms", to={}, text="secret")
    code, _ = bridges_store.create_pair_code(bridge.id)
    row = conv_store.upsert_seen(
        bridge, source="gchat", conversation_id="c1", title="t", is_group=False, link=None,
        speaker="x", people=[], preview="p",
    )
    held_chat_store.create(
        "br_x_1", bridge_id=bridge.id, conversation_id="c1", conv_row_id=row.id,
        family_id="fam-bridge", to_uid="u1", sender_name="x", body="secret",
    )
    paths = (
        f"bridges/{bridge.id}",
        f"bridges/{bridge.id}/outbox/{item.id}",
        f"bridgePairCodes/{code}",
        f"bridgeConversations/{row.id}",
        "heldChat/br_x_1",
    )
    for p in (admin, other_admin, member, None):
        for path in paths:
            assert not _get(path, p), (path, p)
            assert not _write(path, p, {"tokenHash": "hacked"})


def test_device_secrets_is_default_deny(two_pairs):
    _device("pgr-secret-rules-1", "u1")
    device_secrets_store.create("pgr-secret-rules-1", hmac_key=b"k" * 32, mqtt_password_hash="hash")
    assert not _get("deviceSecrets/pgr-secret-rules-1", P("u1"))
    users_store.create_user(uid="admin1", alias="admin1", display_name="Admin", role="admin")
    assert not _get("deviceSecrets/pgr-secret-rules-1", P("admin1"))
    assert not _get("deviceSecrets/pgr-secret-rules-1", None)
    assert not _write("deviceSecrets/pgr-secret-rules-1", P("u1"), {"mqttPasswordHash": "hacked"})


def test_cas_is_default_deny(two_pairs):
    sha_hex = "ab" * 32
    cas_store.remember(sha_hex, "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    assert not _get(f"cas/{sha_hex}", P("u1"))
    users_store.create_user(uid="admin-cas", alias="admincas", display_name="Admin", role="admin")
    assert not _get(f"cas/{sha_hex}", P("admin-cas"))
    assert not _get(f"cas/{sha_hex}", None)
    assert not _write(f"cas/{sha_hex}", P("u1"), {"pem": "hacked"})


def test_cells_is_default_deny(two_pairs):
    cells_store.remember_resolved(
        "310", "410", 12345, 87654321, lat=1.0, lon=2.0, acc_m=1000, provider="google"
    )
    key = cells_store.cache_key("310", "410", 12345, 87654321)
    assert not _get(f"cells/{key}", P("u1"))
    users_store.create_user(uid="admin-cells", alias="admincells", display_name="Admin", role="admin")
    assert not _get(f"cells/{key}", P("admin-cells"))
    assert not _get(f"cells/{key}", None)
    assert not _write(f"cells/{key}", P("u1"), {"lat": 999.0})


def test_contact_request_readable_by_owner_and_family_admin_not_a_third_party(two_pairs):
    from app.store import contacts as contacts_store

    _device("pgr-rules-contacts-1", "u1")
    seed_legacy_contact_request("pgr-rules-contacts-1", "u1", "u_rules1", "Grandma", "+15551230000")
    doc_key = contacts_store.key("pgr-rules-contacts-1", "u_rules1")
    _set_family("contactRequests", doc_key, "famA")
    path = f"contactRequests/{doc_key}"

    assert _get(path, P("u1"))
    assert not _get(path, P("u3"))
    users_store.create_user(
        uid="admin-contacts-1", alias="admincontacts1", display_name="Admin", role="admin"
    )
    assert _get(path, P("admin-contacts-1", "admin", "famA"))
    users_store.create_user(
        uid="admin-contacts-2", alias="admincontacts2", display_name="Admin", role="admin"
    )
    assert not _get(path, P("admin-contacts-2", "admin", "famB"))
    users_store.create_user(uid="bare-admin-contacts-1", alias="bareadmincr1", display_name="Bare")
    assert not _get(path, P("bare-admin-contacts-1"))
    assert not _get(path, None)


def test_sms_log_readable_by_owner_and_family_admin_not_a_third_party(two_pairs):
    from app.store import sms as sms_store

    _device("pgr-rules-sms-1", "u2")
    _set_family("devices", "pgr-rules-sms-1", "famA")
    devices_store.set_locatable_by("pgr-rules-sms-1", ["u1"])
    sms_store.create_log(
        "pgr-rules-sms-1", "s_rules1", ts=1000, sms_ts=1000, dir_="out",
        peer="+15550001111", st="sent", body="hi",
    )
    path = "devices/pgr-rules-sms-1/smsLog/s_rules1"

    assert _get(path, P("u2"))
    assert not _get(path, P("u1"))
    assert not _get(path, P("u3"))
    assert not _get(path, None)
    users_store.create_user(uid="admin-sms-1", alias="adminsms1", display_name="Admin", role="admin")
    assert _get(path, P("admin-sms-1", "admin", "famA"))
    users_store.create_user(uid="admin-sms-2", alias="adminsms2", display_name="Admin", role="admin")
    assert not _get(path, P("admin-sms-2", "admin", "famB"))
    assert not _write(path, P("u2"), {"body": "hacked"})


def test_sms_contacts_field_not_client_writable(two_pairs):
    _device("pgr-rules-sms-2", "u2")
    assert not _write("devices/pgr-rules-sms-2", P("u2"), {"smsContacts": "hacked"})


@pytest.fixture
def group_convo():
    for uid, alias in (("g1", "galice"), ("g2", "gbob"), ("g3", "gcarol"), ("g4", "goutsider")):
        users_store.create_user(uid=uid, alias=alias, display_name=alias)
    conv = conversations_store.create_group(
        name="Family", alias="rules-fam", member_uids=["g1", "g2", "g3"], created_by="g1"
    )
    seq = messages_store.allocate_seq()
    copies = [
        messages_store.create_message(
            sender_uid="g1",
            recipient_uid=to,
            kind="text",
            ts=1000,
            body="hi",
            conv_key=conv.convKey,
            uids=sorted(["g1", to]),
            seq=seq,
            group_msg_id="gm_rules1",
            sender_alias="galice",
        )
        for to in ("g2", "g3")
    ]
    return conv, copies[0], copies[1]


def test_group_member_can_read_conversation_and_own_copy(group_convo):
    conv, copy_to_g2, _ = group_convo
    assert _get(f"conversations/{conv.convKey}", P("g2"))
    assert _get(f"messages/{copy_to_g2.id}", P("g2"))


def test_group_non_member_cannot_read_conversation_or_any_copy(group_convo):
    conv, copy_to_g2, copy_to_g3 = group_convo
    assert not _get(f"conversations/{conv.convKey}", P("g4"))
    assert not _get(f"messages/{copy_to_g2.id}", P("g4"))
    assert not _get(f"messages/{copy_to_g3.id}", P("g4"))


def test_group_member_cannot_read_a_copy_addressed_to_a_different_member(group_convo):
    _, _, copy_to_g3 = group_convo
    assert not _get(f"messages/{copy_to_g3.id}", P("g2"))


def test_group_thread_convkey_only_query_is_denied(group_convo):
    conv, _, _ = group_convo
    q = {"collection": "messages", "where": [["convKey", "==", conv.convKey]]}
    assert _run_query(P("g1"), q) is None


def test_group_sender_can_list_query_both_of_their_own_copies(group_convo):
    conv, _, _ = group_convo
    q = {
        "collection": "messages",
        "where": [["convKey", "==", conv.convKey], ["uids", "array-contains", "g1"]],
    }
    rows = _run_query(P("g1"), q)
    assert rows is not None and len(rows) == 2


def test_former_group_member_keeps_old_copies_but_loses_the_summary(group_convo):
    conv, copy_to_g2, _ = group_convo
    conversations_store.remove_member(conv.convKey, "g2")
    assert _get(f"messages/{copy_to_g2.id}", P("g2"))
    assert not _get(f"conversations/{conv.convKey}", P("g2"))


def test_family_doc_readable_by_same_family_member_and_super_not_other_family(two_pairs):
    _create_family("famA")
    _create_family("famB")
    assert _get("families/famA", P("u2", fam="famA"))
    assert not _get("families/famA", P("u3", fam="famB"))
    users_store.create_user(uid="super-fam-1", alias="superfam1", display_name="Super")
    sup = P("super-fam-1", "super", "famA")
    assert _get("families/famA", sup)
    assert _get("families/famB", sup)


def test_family_alerts_readable_only_by_family_admin_or_super(two_pairs):
    _create_family("famA")
    _set_family("users", "u1", "famA")
    get_db().collection("families").document("famA").collection("alerts").document("a1").set(
        {"kind": "sms_unknown", "status": "open", "ts": 1}
    )
    path = "families/famA/alerts/a1"
    users_store.create_user(uid="admin-alerts-1", alias="adminalerts1", display_name="Admin", role="admin")
    assert _get(path, P("admin-alerts-1", "admin", "famA"))
    users_store.create_user(uid="admin-alerts-2", alias="adminalerts2", display_name="Admin", role="admin")
    assert not _get(path, P("admin-alerts-2", "admin", "famB"))
    assert not _get(path, P("u1"))
    users_store.create_user(uid="super-alerts-1", alias="superalerts1", display_name="Super")
    assert _get(path, P("super-alerts-1", "super", "famA"))


def test_same_family_member_reads_users_doc_other_family_member_denied(two_pairs):
    _set_family("users", "u1", "famA")
    _set_family("users", "u2", "famA")
    _set_family("users", "u3", "famB")
    assert _get("users/u1", P("u2", fam="famA"))
    assert not _get("users/u1", P("u3", fam="famB"))


def test_bare_legacy_admin_claim_grants_nothing(two_pairs):
    _create_family("famA")
    _set_family("users", "u2", "famA")
    _device("pgr-rules-bare-1", "u2")
    _set_family("devices", "pgr-rules-bare-1", "famA")
    allow_store.set_edge("u1", "u2", message=True, locate=False)
    _set_allow_family_ids("u1", "u2", ["famA"])
    users_store.create_user(uid="bare-admin-1", alias="bareadmin1", display_name="Bare")
    bare = P("bare-admin-1")
    assert not _get("families/famA", bare)
    assert not _get("users/u2", bare)
    assert not _get("devices/pgr-rules-bare-1", bare)
    assert not _get("allow/u1_u2", bare)


def test_family_admin_reads_family_device_and_locations_other_family_admin_denied(two_pairs):
    from app.store.locations import LocationFix, add_location

    _device("pgr-rules-famdev-1", "u2")
    _set_family("devices", "pgr-rules-famdev-1", "famA")
    loc_id = add_location("pgr-rules-famdev-1", LocationFix(ts=1, fixTs=1, lat=1.0, lon=2.0))
    loc = f"devices/pgr-rules-famdev-1/locations/{loc_id}"
    users_store.create_user(uid="admin-famdev-1", alias="adminfamdev1", display_name="Admin", role="admin")
    admin = P("admin-famdev-1", "admin", "famA")
    assert _get("devices/pgr-rules-famdev-1", admin)
    assert _get(loc, admin)
    users_store.create_user(uid="admin-famdev-2", alias="adminfamdev2", display_name="Admin", role="admin")
    other = P("admin-famdev-2", "admin", "famB")
    assert not _get("devices/pgr-rules-famdev-1", other)
    assert not _get(loc, other)


def test_allow_edge_readable_by_family_admin_of_either_party_not_a_third_family(two_pairs):
    allow_store.set_edge("u1", "u2", message=True, locate=False)
    _set_allow_family_ids("u1", "u2", ["famA", "famB"])
    for i, (fam, ok) in enumerate((("famA", True), ("famB", True), ("famC", False)), start=1):
        uid = f"admin-allow-{i}"
        users_store.create_user(uid=uid, alias=f"adminallow{i}", display_name="Admin", role="admin")
        assert _get("allow/u1_u2", P(uid, "admin", fam)) is ok


def test_family_admin_reads_cross_family_dm_symmetric_third_family_and_member_denied(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    msg = two_pairs
    _set_family_ids("conversations", key, ["famA", "famB"])
    _set_family_ids("messages", msg.id, ["famA", "famB"])
    for uid, alias, fam, ok in (
        ("admin-conv-a", "adminconva", "famA", True),
        ("admin-conv-b", "adminconvb", "famB", True),
        ("admin-conv-c", "adminconvc", "famC", False),
    ):
        admin = _make_family_admin(uid, alias, fam)
        assert _get(f"conversations/{key}", admin) is ok
        assert _get(f"messages/{msg.id}", admin) is ok
    member = P("u3", fam="famA")
    assert not _get(f"conversations/{key}", member)
    assert not _get(f"messages/{msg.id}", member)


def test_sms_conversation_denied_to_other_family_admin(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    msg = two_pairs
    _set_family_ids("conversations", key, ["famA"])
    _set_family_ids("messages", msg.id, ["famA"])
    same = _make_family_admin("admin-sms-conv-1", "adminsmsconv1", "famA")
    assert _get(f"conversations/{key}", same)
    assert _get(f"messages/{msg.id}", same)
    other = _make_family_admin("admin-sms-conv-2", "adminsmsconv2", "famB")
    assert not _get(f"conversations/{key}", other)
    assert not _get(f"messages/{msg.id}", other)


def test_family_admin_query_by_own_familyIds_allowed_other_family_denied(two_pairs):
    key = messages_store.conv_key("u1", "u2")
    msg = two_pairs
    _set_family_ids("conversations", key, ["famA", "famB"])
    _set_family_ids("messages", msg.id, ["famA", "famB"])
    admin = _make_family_admin("admin-conv-query-1", "adminconvquery1", "famA")
    assert _run_query(admin, _family_ids_query("conversations", "famA"))
    assert _run_query(admin, _family_ids_query("messages", "famA"))
    assert _run_query(admin, _family_ids_query("conversations", "famB")) is None
    assert _run_query(admin, _family_ids_query("messages", "famB")) is None


def test_book_nick_readable_by_owner_and_family_admin_only_writes_denied(two_pairs):
    _set_family("users", "u1", "famA")
    _set_family("users", "u2", "famA")
    get_db().collection("users").document("u1").collection("book").document("u2").set(
        {"nick": "Bro", "familyId": "famA", "updatedBy": "u1"}
    )
    path = "users/u1/book/u2"
    owner = P("u1", fam="famA")
    assert _get(path, owner)
    same_admin = _make_family_admin("admin-book-a", "adminbooka", "famA")
    assert _get(path, same_admin)
    other_admin = _make_family_admin("admin-book-b", "adminbookb", "famB")
    assert not _get(path, other_admin)
    sibling = P("u2", fam="famA")
    assert not _get(path, sibling)
    for p in (owner, same_admin, sibling):
        assert not _write(path, p, {"nick": "Hacked"})
        assert not _write("users/u1/book/u3", p, {"nick": "New"})


def test_book_added_marker_is_not_client_writable(two_pairs):
    _set_family("users", "u1", "famA")
    _set_family("users", "u2", "famA")
    get_db().collection("users").document("u1").collection("book").document("u2").set(
        {"added": True, "familyId": "famA", "addedBy": "pgr-1"}
    )
    path = "users/u1/book/u2"
    owner = P("u1", fam="famA")
    assert _get(path, owner)
    admin = _make_family_admin("admin-book-c", "adminbookc", "famA")
    assert _get(path, admin)
    for p in (owner, admin):
        assert not _write(path, p, {"added": False})
        assert not _write("users/u1/book/u3", p, {"added": True, "familyId": "famA"})


def test_refuses_unauthenticated_query(two_pairs):
    assert not readauth.query_admissible(None, {"collection": "settings"})


def test_refuses_collection_group_query(two_pairs):
    sup = P("u1", "super")
    assert not readauth.query_admissible(sup, {"collection": "locations", "group": True})


def test_refuses_collection_without_a_match_block(two_pairs):
    sup = P("u1", "super")
    assert not readauth.query_admissible(sup, {"collection": "deviceSecrets"})
    assert not readauth.query_admissible(sup, {"collection": "bridges/b1/outbox"})


def test_refuses_families_list_for_non_super(two_pairs):
    q = {"collection": "families"}
    assert readauth.query_admissible(P("u1", "super"), q)
    assert not readauth.query_admissible(P("u1", "admin", "famA"), q)


def test_refuses_alerts_list_for_member_or_other_family_admin(two_pairs):
    q = {"collection": "families/famA/alerts", "where": [["status", "==", "open"]]}
    assert readauth.query_admissible(P("a", "admin", "famA"), q)
    assert not readauth.query_admissible(P("u1", fam="famA"), q)
    assert not readauth.query_admissible(P("a", "admin", "famB"), q)


def test_refuses_users_list_without_own_family_filter(two_pairs):
    member = P("u1", fam="famA")
    assert readauth.query_admissible(member, {"collection": "users", "where": [["familyId", "==", "famA"]]})
    assert not readauth.query_admissible(member, {"collection": "users"})
    assert not readauth.query_admissible(
        member, {"collection": "users", "where": [["familyId", "==", "famB"]]}
    )
    # No family at all: `sameFam` is false even for a matching empty filter.
    assert not readauth.query_admissible(P("u1"), {"collection": "users", "where": [["familyId", "==", ""]]})


def test_refuses_someone_elses_backends(two_pairs):
    assert readauth.query_admissible(P("u1"), {"collection": "users/u1/backends"})
    assert not readauth.query_admissible(P("u1"), {"collection": "users/u2/backends"})


def test_refuses_someone_elses_book_without_admin_family_filter(two_pairs):
    assert readauth.query_admissible(P("u1"), {"collection": "users/u1/book"})
    assert not readauth.query_admissible(P("u1", fam="famA"), {"collection": "users/u2/book"})
    admin = P("a", "admin", "famA")
    assert not readauth.query_admissible(admin, {"collection": "users/u2/book"})
    assert readauth.query_admissible(
        admin, {"collection": "users/u2/book", "where": [["familyId", "==", "famA"]]}
    )


def test_refuses_messages_uids_filter_naming_someone_else(two_pairs):
    q = {"collection": "messages", "where": [["uids", "array-contains", "u2"]]}
    assert not readauth.query_admissible(P("u1"), q)


def test_refuses_conversations_familyIds_filter_for_a_member(two_pairs):
    q = _family_ids_query("conversations", "famA")
    assert not readauth.query_admissible(P("u1", fam="famA"), q)


def test_refuses_messages_query_from_unregistered_uid(two_pairs):
    q = {"collection": "messages", "where": [["uids", "array-contains", "stranger"]]}
    assert not readauth.query_admissible(P("stranger"), q)


def test_refuses_devices_list_without_family_filter(two_pairs):
    q = {"collection": "devices", "where": [["ownerUid", "==", "u1"]]}
    assert not readauth.query_admissible(P("u1", fam="famA"), q)


def test_refuses_devices_list_by_another_owner_for_a_member(two_pairs):
    q = {"collection": "devices", "where": [["familyId", "==", "famA"], ["ownerUid", "==", "u2"]]}
    assert not readauth.query_admissible(P("u1", fam="famA"), q)
    assert readauth.query_admissible(P("a", "admin", "famA"), q)


def test_refuses_locations_of_a_missing_device(two_pairs):
    assert not readauth.query_admissible(P("u1", fam="famA"), {"collection": "devices/nope/locations"})


def test_refuses_sms_log_for_a_locate_grant(two_pairs):
    _device("pgr-adm-sms", "u2")
    _set_family("devices", "pgr-adm-sms", "famA")
    devices_store.set_locatable_by("pgr-adm-sms", ["u1"])
    assert readauth.query_admissible(P("u1", fam="famA"), {"collection": "devices/pgr-adm-sms/locations"})
    assert not readauth.query_admissible(P("u1", fam="famA"), {"collection": "devices/pgr-adm-sms/smsLog"})


def test_refuses_allow_list_without_a_party_or_family_filter(two_pairs):
    assert not readauth.query_admissible(P("u1"), {"collection": "allow"})
    assert readauth.query_admissible(P("u1"), {"collection": "allow", "where": [["fromUid", "==", "u1"]]})
    assert not readauth.query_admissible(
        P("a", "admin", "famA"), _family_ids_query("allow", "famB")
    )


def test_refuses_contact_requests_without_owner_or_admin_family_filter(two_pairs):
    assert not readauth.query_admissible(P("u1"), {"collection": "contactRequests"})
    assert readauth.query_admissible(
        P("a", "admin", "famA"), {"collection": "contactRequests", "where": [["familyId", "==", "famA"]]}
    )


def test_refuses_settings_for_unregistered_uid(two_pairs):
    assert readauth.query_admissible(P("u1"), {"doc": "settings/retention"})
    assert not readauth.query_admissible(P("stranger"), {"doc": "settings/retention"})


def test_refuses_doc_query_the_rule_denies(two_pairs):
    assert readauth.query_admissible(P("u1"), {"doc": f"messages/{two_pairs.id}"})
    assert not readauth.query_admissible(P("u3"), {"doc": f"messages/{two_pairs.id}"})
    # A missing document: `resource.data.uids` on a null resource errors.
    assert not readauth.query_admissible(P("u1"), {"doc": "messages/nope"})


def test_refuses_unsupported_operator(two_pairs):
    q = {"collection": "users", "where": [["familyId", ">=", "famA"]]}
    assert not readauth.query_admissible(P("u1", "super"), q)
