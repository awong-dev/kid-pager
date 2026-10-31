"""`app/homeinit.py`: the home stack's `init` service. Pure file work: no store."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa

from app import homeinit
from app.config import Settings


def _run(data: Path, host: str = "pager.example.org", mode: str = "selfsigned") -> None:
    homeinit.run(data, host, mode)


def _cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_first_run_creates_the_layout(tmp_path: Path):
    _run(tmp_path)
    secrets = homeinit._read_env(tmp_path / "secrets.env")
    assert set(secrets) == {*homeinit.SECRET_KEYS, homeinit.VAPID_PRIVATE, homeinit.VAPID_PUBLIC}
    assert all(secrets.values())
    assert "SESSION_KEY" not in secrets
    assert (tmp_path / "secrets.env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "certs/ca/ca.key").stat().st_mode & 0o777 == 0o600

    ca = _cert(tmp_path / "certs/ca/ca.crt")
    assert ca.subject.rfc4514_string().startswith("CN=Pager Home CA ")
    assert isinstance(ca.public_key(), rsa.RSAPublicKey) and ca.public_key().key_size == 2048
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert ca.not_valid_after_utc - ca.not_valid_before_utc > timedelta(days=3649)

    mqtt = _cert(tmp_path / "certs/mqtt/server.crt")
    assert mqtt.issuer == ca.subject
    mqtt.verify_directly_issued_by(ca)
    san = mqtt.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["pager.example.org"]
    assert mqtt.not_valid_after_utc - mqtt.not_valid_before_utc > timedelta(days=3649)

    chain = x509.load_pem_x509_certificates((tmp_path / "certs/web/fullchain.pem").read_bytes())
    assert len(chain) == 2 and chain[1] == ca
    chain[0].verify_directly_issued_by(ca)
    assert chain[0].not_valid_after_utc - chain[0].not_valid_before_utc <= timedelta(days=826)

    keys = (tmp_path / "emqx/bootstrap_api_keys.txt").read_text().strip()
    assert keys == f"{secrets['BROKER_API_KEY']}:{secrets['BROKER_API_SECRET']}"
    assert (tmp_path / "mosquitto").is_dir() and (tmp_path / "relay").is_dir()

    relay_env = homeinit._read_env(tmp_path / "relay.env")
    assert relay_env == {
        "PUBLIC_BASE_URL": "https://pager.example.org",
        "BROKER_HOST": "pager.example.org",
        "BROKER_CA_PEM_FILE": str(tmp_path / "certs/ca/ca.crt"),
        "SORACOM_BEAM_DESTINATION": "mqtts://pager.example.org:8883",
        "BROKER_MQTT_PASSWORD": "${BROKER_ADMIN_PASSWORD}",
    }

    # The dynsec file's one client is the relay, with BROKER_ADMIN_PASSWORD.
    dynsec = json.loads((tmp_path / "mosquitto/dynamic-security.json").read_text())
    (client,) = dynsec["clients"]
    assert client["username"] == "relay"
    assert [r["rolename"] for r in client["roles"]] == ["admin", "relay"]
    _, scheme, iterations, salt, digest = client["encoded_password"].split("$")
    assert scheme == "7"
    assert base64.b64decode(digest) == hashlib.pbkdf2_hmac(
        "sha512", secrets["BROKER_ADMIN_PASSWORD"].encode(), base64.b64decode(salt), int(iterations), 64
    )
    assert dynsec["defaultACLAccess"] == {
        "publishClientSend": False,
        "publishClientReceive": False,
        "subscribe": False,
        "unsubscribe": True,
    }


def test_second_run_keeps_every_secret_and_certificate(tmp_path: Path):
    _run(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and p.name != "relay.env"}
    _run(tmp_path, host="other.example.org")
    after = {p: p.read_bytes() for p in before}
    assert after == before
    # relay.env is derived, so it follows the new PAGER_HOST.
    assert "https://other.example.org" in (tmp_path / "relay.env").read_text()


def test_a_missing_secret_is_appended_and_existing_values_kept(tmp_path: Path):
    (tmp_path / "secrets.env").write_text("WEBHOOK_KEY=mine\n# a comment\nEXTRA=1")
    _run(tmp_path)
    values = homeinit._read_env(tmp_path / "secrets.env")
    assert values["WEBHOOK_KEY"] == "mine" and values["EXTRA"] == "1"
    assert all(values[k] for k in homeinit.SECRET_KEYS)


def test_letsencrypt_leaves_the_web_certificate_to_certbot(tmp_path: Path):
    _run(tmp_path, mode="letsencrypt")
    assert not (tmp_path / "certs/web").exists()
    assert (tmp_path / "certs/mqtt/server.crt").exists()


def test_an_ip_host_gets_an_ip_san(tmp_path: Path):
    _run(tmp_path, host="203.0.113.7")
    san = _cert(tmp_path / "certs/mqtt/server.crt").extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["203.0.113.7"]


def test_the_web_certificate_is_reissued_near_expiry(tmp_path: Path):
    _run(tmp_path)
    web = tmp_path / "certs/web"
    old = (web / "fullchain.pem").read_bytes()
    ca, ca_key = homeinit.ensure_ca(tmp_path / "certs/ca")
    homeinit.ensure_web_cert(web, ca, ca_key, "pager.example.org")
    assert (web / "fullchain.pem").read_bytes() == old
    later = datetime.now(UTC) + timedelta(days=810)
    homeinit.ensure_web_cert(web, ca, ca_key, "pager.example.org", now=later)
    assert (web / "fullchain.pem").read_bytes() != old


def test_a_half_present_ca_is_refused(tmp_path: Path):
    _run(tmp_path)
    (tmp_path / "certs/ca/ca.key").unlink()
    with pytest.raises(homeinit.InitError, match="only one of"):
        _run(tmp_path)


@pytest.mark.parametrize(("host", "mode"), [("", "selfsigned"), ("h", "public"), ("h", "")])
def test_bad_configuration_exits_nonzero(tmp_path: Path, monkeypatch, host: str, mode: str):
    monkeypatch.setenv("PAGER_HOST", host)
    monkeypatch.setenv("TLS_MODE", mode)
    assert homeinit.main(["--data", str(tmp_path)]) == 1


def test_the_relay_reads_the_ca_from_broker_ca_pem_file(tmp_path: Path, monkeypatch):
    _run(tmp_path)
    for key, value in homeinit._read_env(tmp_path / "relay.env").items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BROKER_CA_PEM", raising=False)
    settings = Settings.from_env()
    assert settings.broker_ca_pem == (tmp_path / "certs/ca/ca.crt").read_text()
    assert settings.broker_host == "pager.example.org"
    monkeypatch.setenv("BROKER_CA_PEM", "inline")
    assert Settings.from_env().broker_ca_pem == "inline"
