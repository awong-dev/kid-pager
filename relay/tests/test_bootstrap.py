
from __future__ import annotations

from firebase_admin import auth as fb_auth

from app.bootstrap import bootstrap_admin
from app.store import users as users_store




    user = users_store.get_user(uid)
    assert user is not None
    assert user.alias == "admin"


def test_bootstrap_is_idempotent():
    uid1 = bootstrap_admin("root2@example.com")
    uid2 = bootstrap_admin("root2@example.com")
    assert uid1 == uid2

    users = users_store.list_users()
    assert sum(1 for u in users if u.uid == uid1) == 1


def test_bootstrap_custom_alias_and_display_name():
    user = users_store.get_user(uid)
    assert user is not None
    assert user.alias == "superadmin"
    assert user.displayName == "Super Admin"
