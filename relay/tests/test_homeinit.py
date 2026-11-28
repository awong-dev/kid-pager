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
        "PUBLIC_BASE_URL": "https://pager.example.org/kid-pager",
        "BROKER_HOST": "pager.example.org",
        "BROKER_PORT": "8883",
        "BEAM_PORT": "",
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


@pytest.mark.parametrize(
    ("raw", "want"),
    [("", "/kid-pager"), ("  ", "/kid-pager"), ("/", ""), ("/pager", "/pager"), ("/pager/", "/pager"), ("/apps/pager_2-x", "/apps/pager_2-x")],
)
def test_normalize_base_path(raw: str, want: str):
    assert homeinit.normalize_base_path(raw) == want


@pytest.mark.parametrize(
    "raw", ["pager", "//pager", "/a//b", "/pa.ger", "/pager?x=1", "/pa ger", "/../x", "/pager//", "/é"]
)
def test_bad_base_path_is_an_init_error(tmp_path: Path, raw: str):
    with pytest.raises(homeinit.InitError, match="PAGER_PATH"):
        homeinit.run(tmp_path, "pager.example.org", "selfsigned", raw)
    assert not (tmp_path / "relay.env").exists()


def test_base_path_is_written_into_public_base_url(tmp_path: Path):
    homeinit.run(tmp_path, "pager.example.org", "selfsigned", "/apps/pager/")
    env = homeinit._read_env(tmp_path / "relay.env")
    assert env["PUBLIC_BASE_URL"] == "https://pager.example.org/apps/pager"
    # Only the web URL carries the prefix; the broker is reached by host.
    assert env["BROKER_HOST"] == "pager.example.org"
    assert env["SORACOM_BEAM_DESTINATION"] == "mqtts://pager.example.org:8883"
    assert Settings.base_path.fget(  # type: ignore[attr-defined]
        Settings(
            broker_api_url="", broker_api_key=None, broker_api_secret=None, webhook_key="",
            dev_mode=False, google_cloud_project=None, firestore_emulator_host=None,
            firebase_auth_emulator_host=None, public_base_url=env["PUBLIC_BASE_URL"],
        )
    ) == "/apps/pager"


def test_main_reads_pager_path_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PAGER_HOST", "pager.example.org")
    monkeypatch.setenv("TLS_MODE", "selfsigned")
    monkeypatch.setenv("PAGER_PATH", "/pager")
    assert homeinit.main(["--data", str(tmp_path)]) == 0
    assert homeinit._read_env(tmp_path / "relay.env")["PUBLIC_BASE_URL"] == "https://pager.example.org/pager"
    monkeypatch.setenv("PAGER_PATH", "/bad.path")
    assert homeinit.main(["--data", str(tmp_path / "x")]) == 1
    monkeypatch.setenv("PAGER_PATH", "/")
    assert homeinit.main(["--data", str(tmp_path)]) == 0
    assert homeinit._read_env(tmp_path / "relay.env")["PUBLIC_BASE_URL"] == "https://pager.example.org"
    monkeypatch.delenv("PAGER_PATH")
    assert homeinit.main(["--data", str(tmp_path)]) == 0
    assert (
        homeinit._read_env(tmp_path / "relay.env")["PUBLIC_BASE_URL"] == "https://pager.example.org/kid-pager"
    )


def test_public_ports_reach_the_relay_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    homeinit.run(
        tmp_path, "home.example.org", "external", "/kid-pager", web_port=24492, mqtt_port=23093, beam_port=30265
    )
    env = homeinit._read_env(tmp_path / "relay.env")
    assert env["PUBLIC_BASE_URL"] == "https://home.example.org:24492/kid-pager"
    assert env["BROKER_PORT"] == "23093"
    assert env["BEAM_PORT"] == "30265"
    assert env["SORACOM_BEAM_DESTINATION"] == "mqtts://home.example.org:30265"
    # external: the web certificate is someone else's, and its mount point exists for nginx
    assert not (tmp_path / "certs/web").exists()
    assert (tmp_path / "certs/external").is_dir()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings.from_env()
    assert (settings.broker_host, settings.broker_port, settings.beam_port) == ("home.example.org", 23093, 30265)
    assert settings.base_path == "/kid-pager"


def test_selfsigned_has_no_beam_port_and_default_ports_are_not_written_into_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    homeinit.run(tmp_path, "home.example.org", "selfsigned", "/", mqtt_port=23093)
    env = homeinit._read_env(tmp_path / "relay.env")
    assert env["PUBLIC_BASE_URL"] == "https://home.example.org"
    assert env["BEAM_PORT"] == ""
    assert env["SORACOM_BEAM_DESTINATION"] == "mqtts://home.example.org:23093"
    monkeypatch.setenv("BEAM_PORT", "8884")  # a stale value must not survive relay.env
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert Settings.from_env().beam_port is None


@pytest.mark.parametrize("raw", ["0", "65536", "https", "-1", "88 83"])
def test_a_bad_port_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str):
    monkeypatch.setenv("PAGER_HOST", "home.example.org")
    monkeypatch.setenv("TLS_MODE", "selfsigned")
    monkeypatch.setenv("PAGER_MQTT_PORT", raw)
    assert homeinit.main(["--data", str(tmp_path)]) == 1


def test_main_reads_the_ports_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PAGER_HOST", "home.example.org")
    monkeypatch.setenv("TLS_MODE", "letsencrypt")
    monkeypatch.setenv("PAGER_WEB_PORT", "24492")
    monkeypatch.setenv("PAGER_MQTT_PORT", "23093")
    monkeypatch.setenv("PAGER_BEAM_PORT", "30265")
    assert homeinit.main(["--data", str(tmp_path)]) == 0
    env = homeinit._read_env(tmp_path / "relay.env")
    assert (env["PUBLIC_BASE_URL"], env["BROKER_PORT"], env["BEAM_PORT"]) == (
        "https://home.example.org:24492/kid-pager", "23093", "30265",
    )
