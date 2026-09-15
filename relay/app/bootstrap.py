"""`python -m app.bootstrap --admin-email <email>`: make an empty database usable.

from __future__ import annotations

import argparse
import sys

from firebase_admin import auth as fb_auth

from app.db.firestore import get_app
from app.store import users as users_store

DEFAULT_ADMIN_ALIAS = "admin"
DEFAULT_ADMIN_DISPLAY_NAME = "Admin"


def bootstrap_admin(
) -> str:
    get_app()  # ensure firebase_admin is initialized before any auth.* call
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
        )

    return uid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--alias", default=DEFAULT_ADMIN_ALIAS)
    parser.add_argument("--display-name", default=DEFAULT_ADMIN_DISPLAY_NAME)
    args = parser.parse_args(argv)

    print(f"admin bootstrapped: uid={uid} email={args.admin_email} alias={args.alias}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
