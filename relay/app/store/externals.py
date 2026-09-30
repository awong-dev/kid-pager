
from __future__ import annotations


import phonenumbers

from app.db.firestore import get_db
from app.store import users as users_store
from app.store.users import User

# Default region "US": a bare 10-digit number resolves the way a US phone is typed.
DEFAULT_REGION = "US"


def normalize_phone(phone: str, *, default_region: str = DEFAULT_REGION) -> str:
    """E.164 for `phone` via `is_possible_number` (555 numbers pass); `ValueError` if not."""
    try:
        parsed = phonenumbers.parse(phone, default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"not a valid phone number: {phone!r}") from exc
    if not phonenumbers.is_possible_number(parsed):
        raise ValueError(f"not a valid phone number: {phone!r}")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)






    e164 = normalize_phone(phone)

    fetched = users_store.get_user(uid)
    assert fetched is not None
    return fetched
