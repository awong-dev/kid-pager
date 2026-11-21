"""`app/fed/client.py`: address check on every request, connecting to the checked address with the
hostname kept for TLS and Host, per-link TLS trust, limits and the error vocabulary."""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import json
import ssl
import threading
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.fed import addr, keys
from app.fed import client as client_mod
from app.store.fed_families import FedFamily
from app.store.fed_links import FedLink, Remote

REMOTE = "f_bbbbbbbbbbbbbbbbbbbb"


def identity() -> FedFamily:
    private, public = keys.generate()
    return FedFamily(familyId="famA", fedId="f_aaaaaaaaaaaaaaaaaaaa", publicKey=public, privateKey=private)


def link(host="peer.example.org", port=443, ca=None) -> FedLink:
    return FedLink(
        id=f"famA_{REMOTE}",
        familyId="famA",
        remote=Remote(fedId=REMOTE, name="B", publicKey=keys.generate()[1], host=host, port=port, tlsCa=ca),
    )


def policy(*ips: str, cloud=False, loopback=False) -> addr.AddressPolicy:
    return addr.AddressPolicy(
        cloud=cloud, resolver=lambda host, port: list(ips), allow_loopback=loopback
    )


def make_ca(cn="pager test CA"):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert, name


def make_leaf(ca_key, ca_name, dns_name: str, ip: str):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            # only the name, never the address: a connection that verified against the
            # IP literal instead of the hostname would fail
            x509.SubjectAlternativeName([x509.DNSName(dns_name)]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_no_ca_means_the_default_verification():
    assert client_mod.tls_context(None) is True
    with pytest.raises(ValueError):
        client_mod.tls_context("-----BEGIN NOPE-----\nzzz\n-----END NOPE-----\n")
    with pytest.raises(ValueError):
        client_mod.tls_context("not a pem")


def test_the_links_ca_is_the_only_trust_root():
    _, cert, _ = make_ca()
    ctx = client_mod.tls_context(pem(cert))
    assert isinstance(ctx, ssl.SSLContext) and len(ctx.get_ca_certs()) == 1


def test_a_bundle_trusts_each_certificate_in_it():
    _, a, _ = make_ca("a")
    _, b, _ = make_ca("b")
    ctx = client_mod.tls_context(pem(a) + pem(b))
    assert isinstance(ctx, ssl.SSLContext) and len(ctx.get_ca_certs()) == 2


class Recorder(httpx.BaseTransport):
    def __init__(self, status=200, body=b'{"results": []}', error: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.status, self.body, self.error = status, body, error

    def handle_request(self, request):
        request.read()
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, content=self.body)


def test_the_request_goes_to_the_checked_address_with_the_hostname_kept():
    rec = Recorder()
    c = client_mod.FedClient(transport=rec, policy=policy("93.184.216.34"))
    c.post_inbox(identity(), link(host="peer.example.org", port=8443), [])
    (req,) = rec.requests
    assert req.url.host == "93.184.216.34" and req.url.port == 8443
    assert req.headers["host"] == "peer.example.org:8443"
    assert req.extensions["sni_hostname"] == "peer.example.org"
    assert req.url.path == "/fed/v1/inbox"


def test_an_ipv6_address_is_bracketed():
    rec = Recorder()
    c = client_mod.FedClient(transport=rec, policy=policy("2606:2800:220:1::1"))
    c.post_inbox(identity(), link(), [])
    assert rec.requests[0].url.host == "2606:2800:220:1::1"
    assert rec.requests[0].headers["host"] == "peer.example.org"  # 443: no port


def test_a_second_dns_answer_is_never_asked_for():
    calls: list[str] = []

    def resolver(host, port):
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["169.254.169.254"]

    rec = Recorder()
    c = client_mod.FedClient(
        transport=rec, policy=addr.AddressPolicy(cloud=True, resolver=resolver)
    )
    c.post_inbox(identity(), link(), [])
    assert calls == ["peer.example.org"] and rec.requests[0].url.host == "93.184.216.34"


def test_each_checked_address_is_tried_in_turn():
    class FirstDown(Recorder):
        def handle_request(self, request):
            self.requests.append(request)
            if request.url.host == "93.184.216.34":
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, json={"results": []})

    rec = FirstDown()
    c = client_mod.FedClient(transport=rec, policy=policy("93.184.216.34", "93.184.216.35"))
    c.post_inbox(identity(), link(), [])
    assert [r.url.host for r in rec.requests] == ["93.184.216.34", "93.184.216.35"]


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fe80::1"]
)
def test_every_kind_of_call_is_refused_before_anything_is_sent(ip):
    rec = Recorder()
    c = client_mod.FedClient(transport=rec, policy=policy(ip, cloud=True))
    ident, lnk = identity(), link()
    calls = [
        lambda: c.post_inbox(ident, lnk, []),
        lambda: c.directory(ident, lnk),
        lambda: c.link_call(ident, lnk, "POST", "/fed/v1/moved", b"{}"),
        lambda: c.link_call(ident, lnk, "POST", "/fed/v1/unlink", b"{}"),
        lambda: c.call("POST", "/fed/v1/link", host="peer.example.org", body=b"{}"),
    ]
    for call in calls:
        with pytest.raises(client_mod.FedTransportError) as exc:
            call()
        assert exc.value.code == "address_not_allowed"
    assert rec.requests == []


def test_error_codes():
    ident, lnk = identity(), link()
    p = policy("93.184.216.34")
    for error, code in (
        (httpx.ConnectError("refused"), "unreachable"),
        (httpx.ReadTimeout("slow"), "unreachable"),
    ):
        c = client_mod.FedClient(transport=Recorder(error=error), policy=p)
        with pytest.raises(client_mod.FedTransportError) as exc:
            c.post_inbox(ident, lnk, [])
        assert exc.value.code == code
    assert set(client_mod.ERROR_CODES) == {
        "unreachable",
        "tls",
        "refused",
        "address_not_allowed",
        "bad_response",
    }


def test_a_tls_failure_is_tls():
    err = httpx.ConnectError("bad cert")
    err.__cause__ = ssl.SSLCertVerificationError("hostname mismatch")
    c = client_mod.FedClient(transport=Recorder(error=err), policy=policy("93.184.216.34"))
    with pytest.raises(client_mod.FedTransportError) as exc:
        c.post_inbox(identity(), link(), [])
    assert exc.value.code == "tls"


def test_a_response_over_256_kib_is_bad_response():
    c = client_mod.FedClient(
        transport=Recorder(body=b"x" * (256 * 1024 + 1)), policy=policy("93.184.216.34")
    )
    with pytest.raises(client_mod.FedTransportError) as exc:
        c.post_inbox(identity(), link(), [])
    assert exc.value.code == "bad_response"


def test_a_redirect_is_never_followed():
    class Redirect(Recorder):
        def handle_request(self, request):
            self.requests.append(request)
            return httpx.Response(302, headers={"location": "https://169.254.169.254/"})

    rec = Redirect()
    c = client_mod.FedClient(transport=rec, policy=policy("93.184.216.34"))
    with pytest.raises(client_mod.FedTransportError) as exc:
        c.post_inbox(identity(), link(), [])
    assert exc.value.code == "bad_response" and len(rec.requests) == 1


def test_http_errors_and_malformed_answers():
    ident, lnk, p = identity(), link(), policy("93.184.216.34")
    for status, code in ((403, "refused"), (401, "refused"), (503, "unreachable")):
        c = client_mod.FedClient(transport=Recorder(status=status, body=b"{}"), policy=p)
        with pytest.raises(client_mod.FedHttpError) as exc:
            c.post_inbox(ident, lnk, [])
        assert (exc.value.status, exc.value.code) == (status, code)
    c = client_mod.FedClient(transport=Recorder(body=b"not json"), policy=p)
    with pytest.raises(client_mod.FedTransportError) as exc:
        c.post_inbox(ident, lnk, [])
    assert exc.value.code == "bad_response"
    c = client_mod.FedClient(transport=Recorder(body=json.dumps([{"nope": 1}]).encode()), policy=p)
    with pytest.raises(client_mod.FedTransportError):
        c.directory(ident, lnk)


def test_the_request_is_signed_for_the_remote_family():
    from app.fed import sign

    rec = Recorder()
    ident, lnk = identity(), link()
    client_mod.FedClient(transport=rec, policy=policy("93.184.216.34")).post_inbox(ident, lnk, [])
    req = rec.requests[0]
    parsed = sign.parse_headers(req.headers)
    assert (parsed.sender, parsed.receiver) == (ident.fedId, REMOTE)
    sign.verify(
        keys.public_from_b64(ident.publicKey), parsed, "POST", "/fed/v1/inbox", req.content
    )


@pytest.fixture
def tls_server(tmp_path: Path):
    ca_key, ca_cert, ca_name = make_ca()
    seen: list[dict] = []

    def serve(dns_name: str):
        leaf_key, leaf_cert = make_leaf(ca_key, ca_name, dns_name, "127.0.0.1")
        cert_file, key_file = tmp_path / f"{dns_name}.crt", tmp_path / f"{dns_name}.key"
        cert_file.write_text(pem(leaf_cert) + pem(ca_cert))
        key_file.write_bytes(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({"host": self.headers["Host"], "path": self.path})
                body = b"[]"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    servers = []

    def make(dns_name: str):
        s = serve(dns_name)
        servers.append(s)
        return s.server_address[1], pem(ca_cert)

    yield make, seen
    for s in servers:
        s.shutdown()
        s.server_close()


def test_a_real_tls_handshake_verifies_the_hostname_while_connecting_to_the_ip(tls_server):
    make, seen = tls_server
    port, ca = make("peer.test")
    c = client_mod.FedClient(policy=policy("127.0.0.1", loopback=True))
    assert c.directory(identity(), link(host="peer.test", port=port, ca=ca)) == []
    assert seen == [{"host": f"peer.test:{port}", "path": "/fed/v1/directory"}]


def test_a_certificate_for_another_name_is_refused_as_tls(tls_server):
    make, _ = tls_server
    port, ca = make("someone-else.test")
    c = client_mod.FedClient(policy=policy("127.0.0.1", loopback=True))
    with pytest.raises(client_mod.FedTransportError) as exc:
        c.directory(identity(), link(host="peer.test", port=port, ca=ca))
    assert exc.value.code == "tls"


def test_an_untrusted_ca_is_refused_as_tls(tls_server):
    make, _ = tls_server
    port, _ca = make("peer.test")
    _, other_ca, _ = make_ca("another CA")
    c = client_mod.FedClient(policy=policy("127.0.0.1", loopback=True))
    for ca in (pem(other_ca), None):  # a different private CA; and the system store
        with pytest.raises(client_mod.FedTransportError) as exc:
            c.directory(identity(), link(host="peer.test", port=port, ca=ca))
        assert exc.value.code == "tls"


def test_loopback_is_not_reachable_without_the_injection(tls_server):
    make, seen = tls_server
    port, ca = make("peer.test")
    c = client_mod.FedClient(
        policy=addr.AddressPolicy(cloud=False, resolver=lambda h, p: ["127.0.0.1"])
    )
    with pytest.raises(client_mod.FedTransportError) as exc:
        c.directory(identity(), link(host="peer.test", port=port, ca=ca))
    assert exc.value.code == "address_not_allowed" and seen == []
    assert ipaddress.ip_address("127.0.0.1").is_loopback
