"""Re-exports the credential manager from `app/broker_admin.py` under its old import path."""

from app.broker_admin import (  # noqa: F401
    EmqxAdmin,
    EmqxResult,
    _boot_rules,
    _boot_username,
    _broker_manages_auth,
    _device_rules,
)
