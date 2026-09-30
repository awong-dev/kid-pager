"""`python -m app.bootstrap --admin-email <email>`: make an empty database usable.
Creates `families/default`, the super user, its claims and `settings/meta`; idempotent."""

from __future__ import annotations

import argparse
import sys

from firebase_admin import auth as fb_auth

from app.auth import set_claims
from app.db.firestore import get_app
from app.store import families as families_store
from app.store import settings as settings_store
from app.store import users as users_store

DEFAULT_ADMIN_ALIAS = "admin"
DEFAULT_ADMIN_DISPLAY_NAME = "Admin"
DEFAULT_FAMILY_ID = "default"
DEFAULT_FAMILY_NAME = "Home"


def bootstrap_admin(
    admin_email: str,
    *,
    alias: str = DEFAULT_ADMIN_ALIAS,
    display_name: str = DEFAULT_ADMIN_DISPLAY_NAME,
    family_name: str = DEFAULT_FAMILY_NAME,
) -> str:
    """Returns the bootstrap super-admin's uid; safe to call more than once."""
    get_app()  # ensure firebase_admin is initialized before any auth.* call

    if families_store.get_family(DEFAULT_FAMILY_ID) is None:
        families_store.create_family(
            name=family_name, created_by="bootstrap", family_id=DEFAULT_FAMILY_ID
        )

    try:
        auth_user = fb_auth.get_user_by_email(admin_email)
    except fb_auth.UserNotFoundError:
        auth_user = fb_auth.create_user(email=admin_email)

    uid = auth_user.uid
    user = users_store.get_user(uid)
    if user is None:
        user = users_store.create_user(
            uid=uid,
            alias=alias,
            display_name=display_name,
            email=admin_email,
            role="super",
            family_id=DEFAULT_FAMILY_ID,
            kind="person",
        )
    elif user.role != "super":
        user = users_store.update_user(uid, role="super")

    set_claims(uid, "super", DEFAULT_FAMILY_ID)
    settings_store.ensure_meta_initialized()
    return uid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--alias", default=DEFAULT_ADMIN_ALIAS)
    parser.add_argument("--display-name", default=DEFAULT_ADMIN_DISPLAY_NAME)
    parser.add_argument("--family-name", default=DEFAULT_FAMILY_NAME)
    args = parser.parse_args(argv)

    uid = bootstrap_admin(
        args.admin_email,
        alias=args.alias,
        display_name=args.display_name,
        family_name=args.family_name,
    )
    print(f"admin bootstrapped: uid={uid} email={args.admin_email} alias={args.alias}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
