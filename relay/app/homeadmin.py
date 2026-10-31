"""`pager-admin` (`python -m app.homeadmin`): `reset-password <alias>` prints a one-time invite code
for a locked-out account. The relay holds the SQLite file exclusively, so stop it first."""

from __future__ import annotations

import argparse
import sys

from app import authn_home
from app.db.sqlitestore import DatabaseInUse
from app.store import users as users_store


def reset_password(alias: str) -> int:
    try:
        user = users_store.get_user_by_alias(alias)
    except DatabaseInUse:
        print(
            "the relay is running and holds the database. From home/, run:\n"
            "  docker compose stop relay\n"
            f"  docker compose run --rm relay python -m app.homeadmin reset-password {alias}\n"
            "  docker compose start relay",
            file=sys.stderr,
        )
        return 2
    if user is None:
        print(f"no such user: {alias!r}", file=sys.stderr)
        return 1
    code, expires = authn_home.mint_invite(user.uid, "pager-admin")
    print(f"one-time code for {alias}: {code} (valid until {expires:%Y-%m-%d %H:%M} UTC)")
    print("enter it under 'Have an invite code?' on the sign-in page.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pager-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-password", help="print a one-time sign-in code for <alias>")
    reset.add_argument("alias")
    args = parser.parse_args(argv)
    if args.command == "reset-password":
        return reset_password(args.alias)
    return 2


if __name__ == "__main__":
    sys.exit(main())
