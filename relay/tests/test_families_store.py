"""Direct unit tests of `app.store.families` against the Firestore emulator."""

from __future__ import annotations

import pytest

from app.store import families as families_store


def test_create_and_get_family_assigns_id_when_not_given():
    family = families_store.create_family(name="Home", created_by="root-uid")
    assert family.id
    assert family.name == "Home"
    assert family.blockedNumbers == []
    assert family.createdBy == "root-uid"

    fetched = families_store.get_family(family.id)
    assert fetched == family


def test_create_family_with_pinned_id_is_idempotent_key():
    family = families_store.create_family(
        name="Home", created_by="root-uid", family_id="default"
    )
    assert family.id == "default"


def test_get_family_missing_returns_none():
    assert families_store.get_family("no-such-family") is None


def test_list_families():
    families_store.create_family(name="List A", created_by="root-uid")
    families_store.create_family(name="List B", created_by="root-uid")
    names = {f.name for f in families_store.list_families()}
    assert {"List A", "List B"} <= names


def test_update_family_name():
    family = families_store.create_family(name="Old Name", created_by="root-uid")
    updated = families_store.update_family(family.id, name="New Name")
    assert updated.name == "New Name"


def test_update_family_missing_raises():
    with pytest.raises(KeyError):
        families_store.update_family("no-such-family", name="X")


def test_idseed_drives_contact_ids():
    from app.db.firestore import get_db
    from app.store import externals as externals_store

    plain = families_store.create_family(name="Seedless", created_by="r", family_id="orig-fam")
    uid_orig, alias_orig = externals_store.contact_ids(plain.id, "+15550001111")
    assert families_store.seed_for("orig-fam") == "orig-fam"

    moved = families_store.create_family(name="Moved", created_by="r", family_id="new-fam")
    get_db().collection("families").document("new-fam").update({"idSeed": "orig-fam"})
    assert families_store.get_family("new-fam").seed == "orig-fam"
    assert externals_store.contact_ids(moved.id, "+15550001111") == (uid_orig, alias_orig)
    ext = externals_store.get_or_create("new-fam", "+15550001111", "Mom")
    assert ext.uid == uid_orig and ext.ownerFamilyId == "new-fam"
    assert externals_store.get_family_contact("new-fam", "+1 555 000 1111").uid == uid_orig
