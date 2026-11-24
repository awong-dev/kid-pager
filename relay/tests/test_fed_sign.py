"""Federation signing: `from` and `to` are inside the signed string."""

from __future__ import annotations

import pytest

from app.fed import keys, sign

A = "f_aaaaaaaaaaaaaaaaaaaa"
B = "f_bbbbbbbbbbbbbbbbbbbb"
C = "f_cccccccccccccccccccc"


@pytest.fixture
def pair():
    private, public = keys.generate()
    return keys.private_from_b64(private), keys.public_from_b64(public)


def signed(private, *, method="POST", path="/fed/v1/inbox", body=b'{"v":1}', ts=1000, to=B):
    return sign.sign(private, A, to, method, path, body, ts=ts)


def check(public, headers, *, method="POST", path="/fed/v1/inbox", body=b'{"v":1}', now=1000):
    parsed = sign.parse_headers(headers)
    sign.verify(public, parsed, method, path, body, now=now)


def test_a_signature_verifies(pair):
    private, public = pair
    check(public, signed(private))


def test_the_canonical_string_is_pinned():
    assert sign.canonical("post", "/p?q=1", 5, A, B, b"") == (
        b"POST\n/p?q=1\n5\n" + A.encode() + b"\n" + B.encode() + b"\n"
        b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


@pytest.mark.parametrize(
    "request_tamper, header_tamper",
    [
        pytest.param({"method": "GET"}, {}, id="method"),
        pytest.param({"path": "/fed/v1/directory"}, {}, id="path"),
        pytest.param({"body": b'{"v":2}'}, {}, id="body"),
        pytest.param({}, {sign.H_TO: C}, id="to_replayed_at_another_family"),
        pytest.param({}, {sign.H_FROM: C}, id="from"),
    ],
)
def test_a_tampered_request_fails(pair, request_tamper, header_tamper):
    private, public = pair
    headers = signed(private)
    headers.update(header_tamper)
    with pytest.raises(sign.SignatureError) as exc:
        check(public, headers, **request_tamper)
    assert exc.value.code == "bad_sig"


def test_another_key_fails(pair):
    private, public = pair
    other = keys.private_from_b64(keys.generate()[0])
    with pytest.raises(sign.SignatureError):
        check(public, signed(other))


@pytest.mark.parametrize("now", [1000 + 301, 1000 - 301])
def test_a_skewed_timestamp_fails(pair, now):
    private, public = pair
    with pytest.raises(sign.SignatureError) as exc:
        check(public, signed(private), now=now)
    assert exc.value.code == "skew"


def test_the_window_edge_passes(pair):
    private, public = pair
    check(public, signed(private), now=1000 + 300)


@pytest.mark.parametrize(
    "drop", [sign.H_FROM, sign.H_TO, sign.H_TS, sign.H_SIG]
)
def test_a_missing_header_is_missing(pair, drop):
    private, _ = pair
    headers = signed(private)
    del headers[drop]
    with pytest.raises(sign.SignatureError) as exc:
        sign.parse_headers(headers)
    assert exc.value.code == "missing"


def test_a_malformed_id_or_signature_is_missing(pair):
    private, _ = pair
    for field, value in (
        (sign.H_FROM, "relay-a.example.com"),  # a server id is no longer an identity
        (sign.H_TO, "F_AAAA"),
        (sign.H_TS, "soon"),
        (sign.H_SIG, "!!!"),
    ):
        headers = signed(private)
        headers[field] = value
        with pytest.raises(sign.SignatureError):
            sign.parse_headers(headers)


def test_a_response_is_not_a_request(pair):
    private, public = pair
    body = b'{"fedId":"x"}'
    headers = sign.sign(private, A, B, sign.RESPONSE_METHOD, "/fed/v1/link", body, ts=1000)
    check(public, headers, method=sign.RESPONSE_METHOD, path="/fed/v1/link", body=body)
    with pytest.raises(sign.SignatureError):
        check(public, headers, method="POST", path="/fed/v1/link", body=body)


def test_fingerprint_and_ids():
    private, public = keys.generate()
    assert keys.public_of(private) == public
    fp = keys.fingerprint(public)
    assert len(fp.split(" ")) == 16 and all(len(g) == 4 for g in fp.split(" "))
    ids = {keys.new_fed_id() for _ in range(50)}
    assert len(ids) == 50 and all(keys.FED_ID_RE.match(i) for i in ids)
    with pytest.raises(ValueError):
        keys.fingerprint("AAAA")
    with pytest.raises(ValueError):
        keys.public_from_b64("not base64!")
