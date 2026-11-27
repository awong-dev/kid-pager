"""`app/fed/client.py`: address check on every request, connecting to the checked address with the
hostname kept for TLS and Host, per-link TLS trust, limits and the error vocabulary."""

from __future__ import annotations

import datetime
import http.server
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


def link(host="peer.example.org", port=443, ca=None, path="") -> FedLink:
    return FedLink(
        id=f"famA_{REMOTE}",
        familyId="famA",
        remote=Remote(fedId=REMOTE, name="B", publicKey=keys.generate()[1], host=host, port=port, path=path, tlsCa=ca),
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


def test_tls_context_trusts_only_the_links_ca_bundle():
    assert client_mod.tls_context(None) is True
    with pytest.raises(ValueError):
        client_mod.tls_context("-----BEGIN NOPE-----\nzzz\n-----END NOPE-----\n")
    with pytest.raises(ValueError):
        client_mod.tls_context("not a pem")

    _, a, _ = make_ca("a")
    _, b, _ = make_ca("b")
    ctx = client_mod.tls_context(pem(a))
    assert isinstance(ctx, ssl.SSLContext) and len(ctx.get_ca_certs()) == 1
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


@pytest.mark.parametrize(
    ("ip", "port", "host_header"),
    [
        ("93.184.216.34", 8443, "peer.example.org:8443"),
        ("2606:2800:220:1::1", 443, "peer.example.org"),  # bracketed; 443: no port
    ],
    ids=["ipv4", "ipv6"],
)
def test_the_request_goes_to_the_checked_address_with_the_hostname_kept(ip, port, host_header):
    rec = Recorder()
    c = client_mod.FedClient(transport=rec, policy=policy(ip))
    c.post_inbox(identity(), link(host="peer.example.org", port=port), [])
    (req,) = rec.requests
    assert req.url.host == ip and req.url.port == (port if port != 443 else None)
    assert req.headers["host"] == host_header
    assert req.extensions["sni_hostname"] == "peer.example.org"
    assert req.url.path == "/fed/v1/inbox"


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


def test_every_kind_of_call_is_refused_before_anything_is_sent():
    rec = Recorder()
    c = client_mod.FedClient(transport=rec, policy=policy("169.254.169.254", cloud=True))
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


class Redirect(Recorder):
    def handle_request(self, request):
        self.requests.append(request)
        return httpx.Response(302, headers={"location": "https://169.254.169.254/"})


def _tls_failure() -> Recorder:
    err = httpx.ConnectError("bad cert")
    err.__cause__ = ssl.SSLCertVerificationError("hostname mismatch")
    return Recorder(error=err)


@pytest.mark.parametrize(
    ("transport", "status", "code"),
    [
        (lambda: Recorder(error=httpx.ConnectError("refused")), None, "unreachable"),
        (lambda: Recorder(error=httpx.ReadTimeout("slow")), None, "unreachable"),
        (_tls_failure, None, "tls"),
        (lambda: Recorder(body=b"x" * (256 * 1024 + 1)), None, "bad_response"),
        (Redirect, None, "bad_response"),
        (lambda: Recorder(status=403, body=b"{}"), 403, "refused"),
        (lambda: Recorder(status=401, body=b"{}"), 401, "refused"),
        (lambda: Recorder(status=503, body=b"{}"), 503, "unreachable"),
        (lambda: Recorder(body=b"not json"), None, "bad_response"),
    ],
    ids=[
        "connect_error",
        "read_timeout",
        "tls_failure",
        "response_over_256_kib",
        "redirect_never_followed",
        "http_403",
        "http_401",
        "http_503",
        "not_json",
    ],
)
def test_transport_faults_and_responses_map_to_error_codes(transport, status, code):
    rec = transport()
    c = client_mod.FedClient(transport=rec, policy=policy("93.184.216.34"))
    with pytest.raises(
        client_mod.FedHttpError if status else client_mod.FedTransportError
    ) as exc:
        c.post_inbox(identity(), link(), [])
    assert exc.value.code == code
    if status:
        assert exc.value.status == status
    if isinstance(rec, Redirect):
        assert len(rec.requests) == 1


def test_error_vocabulary_and_a_malformed_directory_answer():
    assert set(client_mod.ERROR_CODES) == {
        "unreachable",
        "tls",
        "refused",
        "address_not_allowed",
        "bad_response",
    }
    c = client_mod.FedClient(
        transport=Recorder(body=json.dumps([{"nope": 1}]).encode()),
        policy=policy("93.184.216.34"),
    )
    with pytest.raises(client_mod.FedTransportError):
        c.directory(identity(), link())


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


@pytest.mark.parametrize(
    ("served", "trust"),
    [("someone-else.test", ("own",)), ("peer.test", ("other", "system"))],
    ids=["certificate_for_another_name", "untrusted_ca"],
)
def test_a_real_tls_handshake_that_does_not_verify_is_refused_as_tls(tls_server, served, trust):
    make, _ = tls_server
    port, own_ca = make(served)
    cas = {"own": own_ca, "other": pem(make_ca("another CA")[1]), "system": None}
    c = client_mod.FedClient(policy=policy("127.0.0.1", loopback=True))
    for name in trust:  # a different private CA; and the system store
        with pytest.raises(client_mod.FedTransportError) as exc:
            c.directory(identity(), link(host="peer.test", port=port, ca=cas[name]))
        assert exc.value.code == "tls"


def test_the_url_carries_the_peers_path_but_the_signature_covers_the_bare_route():
    from app.fed import sign

    rec = Recorder()
    c = client_mod.FedClient(transport=rec, policy=policy("93.184.216.34"))
    ident = identity()
    c.post_inbox(ident, link(path="/apps/pager"), [])
    (req,) = rec.requests
    assert req.url.path == "/apps/pager/fed/v1/inbox"
    assert req.headers["host"] == "peer.example.org" and req.url.host == "93.184.216.34"
    parsed = sign.parse_headers(req.headers)
    pub = keys.public_from_b64(ident.publicKey)
    body = req.content
    sign.verify(pub, parsed, "POST", "/fed/v1/inbox", body)
    with pytest.raises(sign.SignatureError):
        sign.verify(pub, parsed, "POST", "/apps/pager/fed/v1/inbox", body)


def test_public_get_trusts_the_given_ca_and_still_checks_the_hostname(tls_server):
    make, seen = tls_server
    port, own_ca = make("peer.test")
    c = client_mod.FedClient(policy=policy("127.0.0.1", loopback=True))
    reply = c.public_get("/api/public/move-info", host="peer.test", port=port, tls_ca=own_ca)
    assert reply.status == 200
    assert seen == [{"host": f"peer.test:{port}", "path": "/api/public/move-info"}]
    # public roots only, another CA, or a certificate for another name: refused as `tls`
    other = pem(make_ca("another CA")[1])
    for host_port, ca in ((port, None), (port, other)):
        with pytest.raises(client_mod.FedTransportError) as exc:
            c.public_get("/api/public/move-info", host="peer.test", port=host_port, tls_ca=ca)
        assert exc.value.code == "tls"
    port2, ca2 = make("someone-else.test")
    with pytest.raises(client_mod.FedTransportError) as exc:
        c.public_get("/api/public/move-info", host="peer.test", port=port2, tls_ca=ca2)
    assert exc.value.code == "tls"


def test_public_get_refuses_a_ca_without_a_certificate():
    c = client_mod.FedClient(policy=policy("93.184.216.34"))
    with pytest.raises(client_mod.FedTransportError) as exc:
        c.public_get("/api/public/move-info", host="peer.test", tls_ca="not a pem")
    assert exc.value.code == "tls"
