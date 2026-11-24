"""`app/store/device_secrets.py`."""

from __future__ import annotations

import base64

import pytest

from app.store import device_secrets as device_secrets_store


def test_create_returns_the_expected_shape_and_stores_base64_key():
    key = bytes(range(32))
    secret = device_secrets_store.create("pgr-secret-1", hmac_key=key, mqtt_password_hash="hash1")
    assert secret.id == "pgr-secret-1"
    assert secret.hmacKey == key
    assert secret.mqttPasswordHash == "hash1"
    assert secret.upN == 0
    assert secret.upBits == 0
    assert secret.downN == 0
    assert secret.sigFailures == 0
    assert secret.createdAt is not None
    assert secret.rotatedAt is None

    fetched = device_secrets_store.get("pgr-secret-1")
    assert fetched is not None
    assert fetched.hmacKey == key
    # The document stores base64 text, not raw bytes.
    from app.db.firestore import get_db

    raw = get_db().collection("deviceSecrets").document("pgr-secret-1").get().to_dict()
    assert raw is not None
    assert base64.b64decode(raw["hmacKey"]) == key


def test_rotate_replaces_secrets_and_zeroes_counters():
    device_secrets_store.create("pgr-secret-3", hmac_key=b"a" * 32, mqtt_password_hash="old")
    device_secrets_store.accept_up_n("pgr-secret-3", 5)
    device_secrets_store.next_down_n("pgr-secret-3")
    device_secrets_store.bump_sig_failures("pgr-secret-3")

    rotated = device_secrets_store.rotate("pgr-secret-3", b"b" * 32, "new")
    assert rotated.hmacKey == b"b" * 32
    assert rotated.mqttPasswordHash == "new"
    assert rotated.upN == 0
    assert rotated.upBits == 0
    assert rotated.downN == 0
    assert rotated.sigFailures == 0
    assert rotated.rotatedAt is not None


def test_delete_removes_the_document():
    device_secrets_store.create("pgr-secret-4", hmac_key=b"c" * 32, mqtt_password_hash="h")
    device_secrets_store.delete("pgr-secret-4")
    assert device_secrets_store.get("pgr-secret-4") is None


def test_accept_up_n_rejects_an_exact_replay_of_the_current_top():
    device_secrets_store.create("pgr-win-2", hmac_key=b"k" * 32, mqtt_password_hash="h")
    assert device_secrets_store.accept_up_n("pgr-win-2", 10) is True
    assert device_secrets_store.accept_up_n("pgr-win-2", 10) is False
    secret = device_secrets_store.get("pgr-win-2")
    assert secret is not None
    assert secret.upN == 10


def test_accept_up_n_fills_a_gap_left_by_reordering_exactly_once():
    device_secrets_store.create("pgr-win-3", hmac_key=b"k" * 32, mqtt_password_hash="h")
    assert device_secrets_store.accept_up_n("pgr-win-3", 1) is True
    assert device_secrets_store.accept_up_n("pgr-win-3", 3) is True  # 2 skipped
    assert device_secrets_store.accept_up_n("pgr-win-3", 2) is True  # gap filled
    assert device_secrets_store.accept_up_n("pgr-win-3", 2) is False  # replay of the fill
    assert device_secrets_store.accept_up_n("pgr-win-3", 1) is False  # replay of the original


def test_accept_up_n_rejects_a_value_older_than_the_64_wide_window():
    device_secrets_store.create("pgr-win-4", hmac_key=b"k" * 32, mqtt_password_hash="h")
    device_secrets_store.accept_up_n("pgr-win-4", 1000)
    # 1000 - 64 = 936 is the oldest value still inside the window.
    assert device_secrets_store.accept_up_n("pgr-win-4", 936) is True
    assert device_secrets_store.accept_up_n("pgr-win-4", 935) is False


def test_accept_up_n_replay_of_a_value_that_slid_out_of_the_bitmap_is_still_caught():
    device_secrets_store.create("pgr-win-5", hmac_key=b"k" * 32, mqtt_password_hash="h")
    assert device_secrets_store.accept_up_n("pgr-win-5", 100) is True
    assert device_secrets_store.accept_up_n("pgr-win-5", 100 + (1 << 20)) is True
    # The old top (100) is outside the 64-wide window: rejected as too old.
    assert device_secrets_store.accept_up_n("pgr-win-5", 100) is False


def test_next_down_n_increments_from_one():
    device_secrets_store.create("pgr-down-1", hmac_key=b"k" * 32, mqtt_password_hash="h")
    assert device_secrets_store.next_down_n("pgr-down-1") == 1
    assert device_secrets_store.next_down_n("pgr-down-1") == 2
    assert device_secrets_store.next_down_n("pgr-down-1") == 3
    secret = device_secrets_store.get("pgr-down-1")
    assert secret is not None
    assert secret.downN == 3


def test_bump_sig_failures_increments_and_returns_the_new_count():
    device_secrets_store.create("pgr-sig-1", hmac_key=b"k" * 32, mqtt_password_hash="h")
    assert device_secrets_store.bump_sig_failures("pgr-sig-1") == 1
    assert device_secrets_store.bump_sig_failures("pgr-sig-1") == 2
    secret = device_secrets_store.get("pgr-sig-1")
    assert secret is not None
    assert secret.sigFailures == 2


@pytest.mark.parametrize(
    "call",
    [
        lambda: device_secrets_store.rotate("pgr-secret-nope", b"x" * 32, "h"),
        lambda: device_secrets_store.accept_up_n("pgr-win-missing", 1),
        lambda: device_secrets_store.next_down_n("pgr-down-missing"),
        lambda: device_secrets_store.bump_sig_failures("pgr-sig-missing"),
    ],
    ids=["rotate", "accept_up_n", "next_down_n", "bump_sig_failures"],
)
def test_store_functions_raise_key_error_for_a_missing_device(call):
    with pytest.raises(KeyError):
        call()
