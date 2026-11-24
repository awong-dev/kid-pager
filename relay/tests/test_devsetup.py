"""Unit tests for `app.devsetup` and `app.store.setup_codes` (`tools/setup_code_vectors.json`)."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cbor2
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import ca_resolve, devsetup
from app.config import Settings
from app.store import cas as cas_store
from app.store import setup_codes as setup_codes_store
from tests.fake_transport import FakeBrokerClient

VECTORS_PATH = Path(__file__).resolve().parents[2] / "tools" / "setup_code_vectors.json"
VECTORS: list[dict[str, Any]] = json.loads(VECTORS_PATH.read_text())
VECTOR = VECTORS[0]
# Second vector: pointer bundle (`ca_url`/`ca_sha`). The firmware host test reads only the first
# occurrence of each field name, so this entry uses different CA field names.
POINTER_VECTOR = next(v for v in VECTORS if v["label"] == "boot_bundle_ca_pointer")


def _settings(*, public_base_url: str | None = None) -> Settings:
    return Settings(
        broker_api_url="http://emqx.test/api/v5",
        broker_api_key="k",
        broker_api_secret="s",
        webhook_key="wk",
        dev_mode=False,
        google_cloud_project=None,
        firestore_emulator_host=None,
        firebase_auth_emulator_host=None,
        public_base_url=public_base_url,
    )


@dataclass
class _FakeEmqx:
    """Records `ensure_boot_user`/`delete_user` calls."""

    ensured: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def ensure_boot_user(self, bid: str, password: str) -> str:
        self.ensured.append((bid, password))
        return "ok"

    def delete_user(self, username: str) -> str:
        self.deleted.append(username)
        return "ok"


def test_token_round_trips_through_format_and_parse():
    raw, token_str = devsetup.token()
    assert len(token_str) == devsetup.TOKEN_CHARS + 1
    code = devsetup.format_code(token_str, host="broker.example.com")
    parsed_raw, host, port, apn = devsetup.parse(code)
    assert parsed_raw == raw
    assert host == "broker.example.com"
    assert port == devsetup.DEFAULT_PORT
    assert apn is None


def test_parse_accepts_spaces_hyphens_mixed_case_port_and_apn():
    raw, token_str = devsetup.token()
    code = devsetup.format_code(token_str, host="a.b-c.example.com", port=1883, apn="fast.t-mobile.com")
    # §3.1: entry must tolerate lowercase, spaces for hyphens and extra whitespace.
    token_part, rest = code.split("@", 1)
    mangled_token = token_part.strip().lower().replace("-", " ")
    mangled_code = f"  {mangled_token}   @{rest}"
    parsed_raw, host, port, apn = devsetup.parse(mangled_code)
    assert parsed_raw == raw
    assert host == "a.b-c.example.com"
    assert port == 1883
    assert apn == "fast.t-mobile.com"


def _flip_first_char(token_part: str) -> str:
    return ("0" if token_part[0] != "0" else "1") + token_part[1:]


def _swap_first_two_chars(token_part: str) -> str:
    chars = list(token_part.replace("-", ""))
    if chars[0] == chars[1]:
        pytest.skip("degenerate token for this property, regenerate")
    chars[0], chars[1] = chars[1], chars[0]
    return "".join(chars)


@pytest.mark.parametrize(
    "mangle", [_flip_first_char, _swap_first_two_chars], ids=["one_character", "transposition"]
)
def test_parse_rejects_a_typo_in_the_token_by_its_check_character(mangle):
    _, token_str = devsetup.token()
    code = devsetup.format_code(token_str, host="broker.example.com")
    token_part, rest = code.split("@", 1)
    with pytest.raises(ValueError, match="check character"):
        devsetup.parse(f"{mangle(token_part)}@{rest}")


@pytest.mark.parametrize(
    ("code", "match"),
    [
        ("0000-0000-0000-00", "@"),
        ("UUUU-UUUU-UUUU-UU @ broker.example.com", None),
    ],
    ids=["missing_at_sign", "invalid_character"],
)
def test_parse_rejects_malformed_codes(code: str, match: str | None):
    with pytest.raises(ValueError, match=match):
        devsetup.parse(code)


def test_derive_matches_the_vector():
    token_bytes = base64.b64decode(VECTOR["token_b64"])
    bid, bpw, bkey = devsetup.derive(token_bytes)
    assert bid == VECTOR["bid"]
    assert bpw == VECTOR["bpw"]
    assert bkey == base64.b64decode(VECTOR["bkey_b64"])


def test_encode_token_matches_the_vectors_code():
    token_bytes = base64.b64decode(VECTOR["token_b64"])
    token_str = devsetup._encode_token(token_bytes)
    expected_token_part = VECTOR["code"].split("@", 1)[0].strip()
    assert expected_token_part.replace("-", "") == token_str


def _vector_device() -> devsetup.BootstrapDevice:
    obj = VECTOR["bundle_plain_obj"]
    return devsetup.BootstrapDevice(
        id=obj["id"],
        pw=obj["pw"],
        k=base64.b64decode(obj["k_b64"]),
        host=obj["host"],
        port=obj["port"],
        flags=obj["flags"],
        label=obj["label"],
    )


_RAW_BUNDLE_KEYS = {
    "id": 1,
    "pw": 30,
    "k": 31,
    "host": 32,
    "port": 33,
    "ca": 34,
    "flags": 35,
    "label": 36,
    "ca_url": 40,
    "ca_sha": 41,
}


@pytest.mark.parametrize("vector", [VECTOR, POINTER_VECTOR], ids=["inline_ca", "ca_pointer"])
def test_vector_bundle_decrypts_to_the_plaintext_object_and_by_hand(vector: dict[str, Any]):
    """By hand: AESGCM and bare `cbor2`, raw integer keys per PROTOCOL.md §10."""
    bkey = base64.b64decode(vector["bkey_b64"])
    blob = base64.b64decode(vector["bundle_ct_b64"])
    expected = {
        k.removesuffix("_b64"): (
            base64.b64decode(v) if k.endswith("_b64") else v
        )
        for k, v in vector["bundle_plain_obj"].items()
    }
    assert devsetup.decrypt_bundle(bkey, blob) == expected

    nonce, ct = blob[:12], blob[12:]
    assert nonce == base64.b64decode(vector["bundle_nonce_b64"])
    raw = cbor2.loads(AESGCM(bkey).decrypt(nonce, ct, None))
    assert raw == {0: 1} | {_RAW_BUNDLE_KEYS[k]: v for k, v in expected.items() if k != "v"}


def test_bundle_round_trips_with_a_fresh_random_nonce():
    device = devsetup.BootstrapDevice(
        id="pgr-0002",
        pw="pw",
        k=b"k" * 32,
        host="broker.example.com",
        ca_url="https://relay.example.com/ca/" + "ab" * 32 + ".pem",
        ca_sha=b"a" * 32,
        flags=0,
        label="Kid 2",
        apn="internet",
    )
    bkey = b"b" * 32
    blob1 = devsetup.bundle(device, bkey)
    blob2 = devsetup.bundle(device, bkey)
    assert blob1 != blob2
    for blob in (blob1, blob2):
        decoded = devsetup.decrypt_bundle(bkey, blob)
        assert decoded == {
            "v": 1,
            "id": "pgr-0002",
            "pw": "pw",
            "k": b"k" * 32,
            "host": "broker.example.com",
            "port": devsetup.DEFAULT_PORT,
            "ca_url": device.ca_url,
            "ca_sha": device.ca_sha,
            "flags": 0,
            "label": "Kid 2",
            "apn": "internet",
        }


def test_bundle_omits_ca_fields_entirely_when_unpinned():
    device = devsetup.BootstrapDevice(
        id="pgr-0003", pw="pw", k=b"k" * 32, host="broker.example.com", flags=0, label="Kid 3"
    )
    bkey = b"b" * 32
    decoded = devsetup.decrypt_bundle(bkey, devsetup.bundle(device, bkey))
    assert "ca_url" not in decoded
    assert "ca_sha" not in decoded
    assert "ca" not in decoded


@pytest.mark.parametrize(
    ("ca_sha", "match"),
    [(None, "ca_url and ca_sha"), (b"too-short", "32-byte")],
    ids=["ca_url_without_ca_sha", "short_ca_sha"],
)
def test_bootstrap_device_rejects_an_invalid_ca_pointer(ca_sha: bytes | None, match: str):
    with pytest.raises(ValueError, match=match):
        devsetup.BootstrapDevice(
            id="pgr-0004",
            pw="pw",
            k=b"k" * 32,
            host="broker.example.com",
            ca_url="https://relay.example.com/ca/" + "ab" * 32 + ".pem",
            ca_sha=ca_sha,
            flags=0,
            label="Kid 4",
        )


def test_decrypt_bundle_wrong_key_raises_invalid_tag():
    device = _vector_device()
    bkey = base64.b64decode(VECTOR["bkey_b64"])
    blob = devsetup.bundle(device, bkey)
    with pytest.raises(InvalidTag):
        devsetup.decrypt_bundle(b"x" * 32, blob)


def _issue(device_id: str, broker: FakeBrokerClient, emqx: _FakeEmqx, **kwargs: Any) -> str:
    settings = kwargs.pop("settings", _settings())
    return devsetup.issue(
        device_id,
        mqtt_password=kwargs.pop("mqtt_password", "real-mqtt-password"),
        hmac_key=kwargs.pop("hmac_key", b"h" * 32),
        host=kwargs.pop("host", "broker.example.com"),
        ca_pem=kwargs.pop("ca_pem", None),
        label=kwargs.pop("label", "Kid 1"),
        settings=settings,
        broker=broker,  # type: ignore[arg-type]
        emqx=emqx,  # type: ignore[arg-type]
        **kwargs,
    )


def test_issue_pushes_boot_user_publishes_retained_bundle_and_writes_setup_code():
    broker = FakeBrokerClient()
    emqx = _FakeEmqx()
    code = _issue(
        "pgr-issue-1",
        broker,
        emqx,
        mqtt_password="p4ssw0rd",
        hmac_key=b"z" * 32,
        host="broker.example.com",
        label="Kid 2",
    )

    assert " @ broker.example.com" in code
    assert len(emqx.ensured) == 1
    bid, _bpw = emqx.ensured[0]
    assert emqx.deleted == []

    assert len(broker.published) == 1
    msg = broker.published[0]
    assert msg.topic == f"pager/boot/{bid}/down"
    assert msg.qos == 1
    assert msg.retain is True
    assert len(msg.payload) > 0

    stored = setup_codes_store.get(bid)
    assert stored is not None
    assert stored.deviceId == "pgr-issue-1"
    assert set(type(stored).model_fields) == {"bid", "deviceId", "expiresAt"}

    raw, host, port, _apn = devsetup.parse(code)
    code_bid, _bpw, bkey = devsetup.derive(raw)
    assert code_bid == bid
    decoded = devsetup.decrypt_bundle(bkey, msg.payload)
    assert decoded["id"] == "pgr-issue-1"
    assert decoded["pw"] == "p4ssw0rd"
    assert decoded["k"] == b"z" * 32
    assert decoded["host"] == host
    assert decoded["port"] == port
    assert decoded["label"] == "Kid 2"
    assert "ca_url" not in decoded and "ca_sha" not in decoded


def test_issue_with_ca_pem_uses_a_pointer_not_inline():
    broker = FakeBrokerClient()
    emqx = _FakeEmqx()
    pem = "-----BEGIN CERTIFICATE-----\nMIIB...FAKE...TEST...CA==\n-----END CERTIFICATE-----\n"
    settings = _settings(public_base_url="https://relay.example.com")
    code = _issue(
        "pgr-issue-ca-1", broker, emqx, ca_pem=pem, settings=settings
    )
    raw, *_ = devsetup.parse(code)
    bid, _bpw, bkey = devsetup.derive(raw)
    decoded = devsetup.decrypt_bundle(bkey, broker.published[0].payload)

    assert "ca" not in decoded
    expected_sha = hashlib.sha256(pem.encode("utf-8")).digest()
    assert decoded["ca_sha"] == expected_sha
    assert decoded["ca_url"] == f"https://relay.example.com/ca/{expected_sha.hex()}.pem"
    assert cas_store.get_pem(expected_sha.hex()) == pem
    assert bid


def test_issue_with_ca_pem_but_no_public_base_url_raises():
    broker = FakeBrokerClient()
    emqx = _FakeEmqx()
    with pytest.raises(ca_resolve.PublicBaseUrlRequired):
        _issue(
            "pgr-issue-ca-2",
            broker,
            emqx,
            ca_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
            settings=_settings(public_base_url=None),
        )
    assert broker.published == []


def test_complete_clears_bundle_revokes_boot_user_and_deletes_setup_code_and_is_repeatable():
    broker = FakeBrokerClient()
    emqx = _FakeEmqx()
    code = _issue("pgr-complete-1", broker, emqx)
    raw, *_ = devsetup.parse(code)
    bid, _bpw, _bkey = devsetup.derive(raw)
    broker.clear()

    devsetup.complete(bid, settings=_settings(), broker=broker, emqx=emqx)

    assert len(broker.published) == 1
    clear_msg = broker.published[0]
    assert clear_msg.topic == f"pager/boot/{bid}/down"
    assert clear_msg.payload == b""
    assert clear_msg.retain is True
    assert emqx.deleted == [f"boot-{bid}"]
    assert setup_codes_store.get(bid) is None

    devsetup.complete(bid, settings=_settings(), broker=broker, emqx=emqx)
    assert setup_codes_store.get(bid) is None


def test_expire_cleans_up_only_codes_past_their_expiresat():
    broker = FakeBrokerClient()
    emqx = _FakeEmqx()
    now = datetime.now(UTC)

    code_old = _issue("pgr-expire-old", broker, emqx, now=now - timedelta(minutes=20))
    code_fresh = _issue("pgr-expire-fresh", broker, emqx, now=now)
    bid_old, _, _ = devsetup.derive(devsetup.parse(code_old)[0])
    bid_fresh, _, _ = devsetup.derive(devsetup.parse(code_fresh)[0])
    broker.clear()

    count = devsetup.expire(now=now, settings=_settings(), broker=broker, emqx=emqx)

    assert count == 1
    assert setup_codes_store.get(bid_old) is None
    assert setup_codes_store.get(bid_fresh) is not None
    assert emqx.deleted == [f"boot-{bid_old}"]
    assert len(broker.published) == 1
    assert broker.published[0].topic == f"pager/boot/{bid_old}/down"
