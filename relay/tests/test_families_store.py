"""Direct unit tests of `app.store.families` against the Firestore emulator."""

from __future__ import annotations

import pytest

from app.store import families as families_store


def test_family_crud():
    family = families_store.create_family(name="Home", created_by="root-uid")
    assert family.id
    assert family.name == "Home"
    assert family.blockedNumbers == []
    assert family.createdBy == "root-uid"
    assert families_store.get_family(family.id) == family

    pinned = families_store.create_family(
        name="Home", created_by="root-uid", family_id="default"
    )
    assert pinned.id == "default"

    assert families_store.get_family("no-such-family") is None

    families_store.create_family(name="List A", created_by="root-uid")
    families_store.create_family(name="List B", created_by="root-uid")
    names = {f.name for f in families_store.list_families()}
    assert {"List A", "List B"} <= names

    updated = families_store.update_family(family.id, name="New Name")
    assert updated.name == "New Name"

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
