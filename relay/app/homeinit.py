"""`python -m app.homeinit`: the home stack's idempotent one-shot `init`; creates missing secrets,
CA and certificates, `mosquitto/dynamic-security.json` and `relay.env` under `data/`."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from py_vapid import Vapid02, b64urlencode

from app.config import valid_base_path

VAPID_PRIVATE, VAPID_PUBLIC = "WEBPUSH_VAPID_PRIVATE_KEY", "WEBPUSH_VAPID_PUBLIC_KEY"
SECRET_KEYS = ("BROKER_ADMIN_PASSWORD", "WEBHOOK_KEY", "BROKER_API_KEY", "BROKER_API_SECRET")
TLS_MODES = ("selfsigned", "letsencrypt", "external")
# The modes whose web certificate a public CA signed: only these have the Soracom Beam listener.
PUBLIC_TLS_MODES = ("letsencrypt", "external")
DEFAULT_WEB_PORT, DEFAULT_MQTT_PORT, DEFAULT_BEAM_PORT = 443, 8883, 8884
TEN_YEARS = timedelta(days=3650)
WEB_CERT_DAYS = timedelta(days=825)
WEB_CERT_RENEW_BEFORE = timedelta(days=30)
EMQX_UID = 1000  # the `emqx` user of emqx/emqx:5.8.0
MOSQUITTO_UID = 1883  # the `mosquitto` user of eclipse-mosquitto:2
DYNSEC_ITERATIONS = 1000  # what `mosquitto_ctrl dynsec init` uses (2.1.2)


class InitError(RuntimeError):
    pass


def _write(path: Path, data: str | bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode() if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    os.chmod(path, mode)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip()
    return values


def ensure_secrets(path: Path) -> dict[str, str]:
    existing = _read_env(path)
    missing = [k for k in SECRET_KEYS if not existing.get(k)]
    if missing:
        text = path.read_text() if path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        for key in missing:
            # token_urlsafe: no `$`, so compose's env_file interpolation
            # leaves the value alone.
            existing[key] = secrets.token_urlsafe(24)
            text += f"{key}={existing[key]}\n"
        _write(path, text, 0o600)
        print(f"homeinit: {path}: generated {', '.join(missing)}")
    if not (existing.get(VAPID_PRIVATE) and existing.get(VAPID_PUBLIC)):
        private, public = vapid_keypair()
        text = "".join(
            line + "\n"
            for line in (path.read_text().splitlines() if path.exists() else [])
            if line.partition("=")[0].strip() not in (VAPID_PRIVATE, VAPID_PUBLIC)
        )
        text += f"{VAPID_PRIVATE}={private}\n{VAPID_PUBLIC}={public}\n"
        existing.update({VAPID_PRIVATE: private, VAPID_PUBLIC: public})
        _write(path, text, 0o600)
        print(f"homeinit: {path}: generated the Web Push VAPID key pair")
    return existing


def vapid_keypair() -> tuple[str, str]:
    """Base64url (no padding) of the raw P-256 private scalar and the uncompressed public point
    (the forms `pywebpush` and `applicationServerKey` take)."""
    vapid = Vapid02()
    vapid.generate_keys()
    private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    public = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64urlencode(private), b64urlencode(public)


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def ensure_ca(ca_dir: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    crt, key_path = ca_dir / "ca.crt", ca_dir / "ca.key"
    if crt.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(crt.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return cert, key
    if crt.exists() or key_path.exists():
        raise InitError(f"{ca_dir} has only one of ca.crt / ca.key; restore the other from a backup")
    key = _key()
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"Pager Home CA {secrets.token_hex(4)}")]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + TEN_YEARS)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write(key_path, _key_pem(key), 0o600)
    _write(crt, _cert_pem(cert), 0o644)
    print(f"homeinit: {crt}: generated ({cert.subject.rfc4514_string()})")
    return cert, key


def _san(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def issue_server_cert(
    ca_cert: x509.Certificate, ca_key: rsa.RSAPrivateKey, host: str, validity: timedelta
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _key()
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + validity)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([_san(host)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def ensure_mqtt_cert(mqtt_dir: Path, ca_cert, ca_key, host: str) -> None:
    crt, key_path = mqtt_dir / "server.crt", mqtt_dir / "server.key"
    if crt.exists() and key_path.exists():
        return
    cert, key = issue_server_cert(ca_cert, ca_key, host, TEN_YEARS)
    _write(key_path, _key_pem(key), 0o600)
    _write(crt, _cert_pem(cert), 0o644)
    print(f"homeinit: {crt}: generated for {host}")


def ensure_web_cert(web_dir: Path, ca_cert, ca_key, host: str, *, now: datetime | None = None) -> None:
    chain, key_path = web_dir / "fullchain.pem", web_dir / "privkey.pem"
    now = now or datetime.now(UTC)
    if chain.exists() and key_path.exists():
        current = x509.load_pem_x509_certificate(chain.read_bytes())
        if current.not_valid_after_utc - now > WEB_CERT_RENEW_BEFORE:
            return
    cert, key = issue_server_cert(ca_cert, ca_key, host, WEB_CERT_DAYS)
    _write(key_path, _key_pem(key), 0o600)
    _write(chain, _cert_pem(cert) + _cert_pem(ca_cert), 0o644)
    print(f"homeinit: {chain}: issued for {host} until {cert.not_valid_after_utc:%Y-%m-%d}")


def ensure_emqx(emqx_dir: Path, values: dict[str, str]) -> None:
    files = {
        emqx_dir / "bootstrap_api_keys.txt": f"{values['BROKER_API_KEY']}:{values['BROKER_API_SECRET']}\n",
        emqx_dir / "emqx.env": f"EMQX_DASHBOARD__DEFAULT_PASSWORD={values['BROKER_ADMIN_PASSWORD']}\n",
    }
    for path, text in files.items():
        if not path.exists():
            _write(path, text, 0o600)
            print(f"homeinit: {path}: written")
        # Read by the EMQX container, which runs as its own user.
        if os.geteuid() == 0:
            os.chown(path, EMQX_UID, EMQX_UID)


def dynsec_password(password: str, *, salt: bytes | None = None) -> str:
    """Mosquitto 2.1's dynamic-security `encoded_password`:
    `$7$<iterations>$<base64 salt>$<base64 PBKDF2-HMAC-SHA512>`, 64-byte salt
    and hash, the form `mosquitto_ctrl dynsec init` writes."""
    salt = salt if salt is not None else secrets.token_bytes(64)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, DYNSEC_ITERATIONS, 64)
    return f"$7${DYNSEC_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _acl(acltype: str, topic: str) -> dict[str, object]:
    return {"acltype": acltype, "topic": topic, "allow": True}


def dynsec_config(relay_password: str) -> dict[str, object]:
    admin_topics = ("$CONTROL/dynamic-security/#", "$SYS/#", "#")
    return {
        "clients": [
            {
                "username": "relay",
                "textName": "pager relay",
                "encoded_password": dynsec_password(relay_password),
                "roles": [{"rolename": "admin"}, {"rolename": "relay"}],
            }
        ],
        "roles": [
            {
                # Verbatim the role `mosquitto_ctrl dynsec init` creates.
                "rolename": "admin",
                "acls": [
                    _acl("publishClientSend", admin_topics[0]),
                    _acl("publishClientReceive", admin_topics[0]),
                    _acl("subscribePattern", admin_topics[0]),
                    _acl("publishClientReceive", admin_topics[1]),
                    _acl("subscribePattern", admin_topics[1]),
                    _acl("publishClientReceive", admin_topics[2]),
                    _acl("subscribePattern", admin_topics[2]),
                    _acl("unsubscribePattern", admin_topics[2]),
                ],
            },
            {
                "rolename": "relay",
                "acls": [
                    _acl("publishClientSend", "pager/#"),
                    _acl("publishClientReceive", "pager/#"),
                    _acl("subscribePattern", "pager/#"),
                ],
            },
        ],
        "defaultACLAccess": {
            "publishClientSend": False,
            "publishClientReceive": False,
            "subscribe": False,
            "unsubscribe": True,
        },
    }


def ensure_dynsec(mosquitto_dir: Path, values: dict[str, str]) -> None:
    path = mosquitto_dir / "dynamic-security.json"
    if not path.exists():
        _write(path, json.dumps(dynsec_config(values["BROKER_ADMIN_PASSWORD"]), indent=2), 0o600)
        print(f"homeinit: {path}: written (client relay)")
    # The broker rewrites the file (via a temp file in the same directory) as clients come and go.
    if os.geteuid() == 0:
        os.chown(mosquitto_dir, MOSQUITTO_UID, MOSQUITTO_UID)
        os.chown(path, MOSQUITTO_UID, MOSQUITTO_UID)


def public_base_url(host: str, base_path: str, web_port: int) -> str:
    port = "" if web_port == DEFAULT_WEB_PORT else f":{web_port}"
    return f"https://{host}{port}{base_path}"


def write_relay_env(
    path: Path,
    data: Path,
    host: str,
    base_path: str = "",
    *,
    web_port: int = DEFAULT_WEB_PORT,
    mqtt_port: int = DEFAULT_MQTT_PORT,
    beam_port: int | None = None,
) -> None:
    """The ports are the ones the outside world dials (the router's side of a forward). `beam_port`
    is `None` when there is no Beam listener."""
    _write(
        path,
        "# Written by app/homeinit.py on every start, from pager.env and the PAGER_*_PORT values in\n"
        "# home/.env. Edit those instead.\n"
        f"PUBLIC_BASE_URL={public_base_url(host, base_path, web_port)}\n"
        f"BROKER_HOST={host}\n"
        f"BROKER_PORT={mqtt_port}\n"
        f"BROKER_CA_PEM_FILE={data / 'certs' / 'ca' / 'ca.crt'}\n"
        "# Empty: no Soracom Beam listener.\n"
        f"BEAM_PORT={beam_port or ''}\n"
        f"SORACOM_BEAM_DESTINATION=mqtts://{host}:{beam_port or mqtt_port}\n"
        "# The relay's MQTT login is the dynsec admin client (BROKER_MODE=mqtt);\n"
        "# expanded from secrets.env when the relay's entrypoint sources this file.\n"
        'BROKER_MQTT_PASSWORD=${BROKER_ADMIN_PASSWORD}\n',
        0o644,
    )


def parse_port(name: str, raw: str, default: int) -> int:
    raw = raw.strip()
    if not raw:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise InitError(f"{name} must be a port number from 1 to 65535, not {raw!r}")
    return int(raw)


# Where the app is mounted when `PAGER_PATH` is unset; home/docker-compose.yml has the same default.
DEFAULT_BASE_PATH = "/kid-pager"


def normalize_base_path(raw: str) -> str:
    """`PAGER_PATH` as `""` (`/`: served at the root) or `/a/b`; empty means the default. No dots,
    because nginx uses it in a regex."""
    value = raw.strip() or DEFAULT_BASE_PATH
    value = value.removesuffix("/")
    if not valid_base_path(value):
        raise InitError(
            f"PAGER_PATH must be / or a path like /kid-pager or /apps/pager (letters, digits, "
            f"'-' and '_' only, no dots, no query), not {raw!r}"
        )
    return value


def run(
    data: Path,
    host: str,
    tls_mode: str,
    base_path: str = "",
    *,
    web_port: int = DEFAULT_WEB_PORT,
    mqtt_port: int = DEFAULT_MQTT_PORT,
    beam_port: int = DEFAULT_BEAM_PORT,
) -> None:
    if not host:
        raise InitError("PAGER_HOST is not set; put it in pager.env (home/README.md)")
    if tls_mode not in TLS_MODES:
        raise InitError(f"TLS_MODE must be one of {TLS_MODES}, not {tls_mode!r}")
    base_path = normalize_base_path(base_path)
    # certs/external is nginx's mount point for TLS_MODE=external and must exist in every mode.
    for sub in ("relay", "mosquitto", "certbot/www", "certs/external"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    values = ensure_secrets(data / "secrets.env")
    ca_cert, ca_key = ensure_ca(data / "certs" / "ca")
    ensure_mqtt_cert(data / "certs" / "mqtt", ca_cert, ca_key, host)
    if tls_mode == "selfsigned":
        ensure_web_cert(data / "certs" / "web", ca_cert, ca_key, host)
    ensure_emqx(data / "emqx", values)
    ensure_dynsec(data / "mosquitto", values)
    beam = beam_port if tls_mode in PUBLIC_TLS_MODES else None
    write_relay_env(
        data / "relay.env", data, host, base_path, web_port=web_port, mqtt_port=mqtt_port, beam_port=beam
    )
    print(
        f"homeinit: {data} ready for {public_base_url(host, base_path, web_port)}/ "
        f"(TLS_MODE={tls_mode}, pagers on port {mqtt_port}"
        + (f", Soracom Beam on port {beam})" if beam else ")")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homeinit", description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=Path(os.environ.get("PAGER_DATA_DIR", "/data")))
    args = parser.parse_args(argv)
    try:
        run(
            args.data,
            (os.environ.get("PAGER_HOST") or "").strip(),
            (os.environ.get("TLS_MODE") or "").strip().lower(),
            os.environ.get("PAGER_PATH") or "",
            web_port=parse_port("PAGER_WEB_PORT", os.environ.get("PAGER_WEB_PORT", ""), DEFAULT_WEB_PORT),
            mqtt_port=parse_port("PAGER_MQTT_PORT", os.environ.get("PAGER_MQTT_PORT", ""), DEFAULT_MQTT_PORT),
            beam_port=parse_port("PAGER_BEAM_PORT", os.environ.get("PAGER_BEAM_PORT", ""), DEFAULT_BEAM_PORT),
        )
    except InitError as exc:
        print(f"homeinit: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
