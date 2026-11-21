"""Outbound address rules."""

from __future__ import annotations

import pytest

from app.fed import addr
from app.fed.addr import AddressNotAllowed, AddressPolicy, check_host


def policy(*ips: str, cloud: bool, loopback: bool = False) -> AddressPolicy:
    return AddressPolicy(
        cloud=cloud, resolver=lambda host, port: list(ips), allow_loopback=loopback
    )


@pytest.mark.parametrize("cloud", [True, False])
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.8.9.10",
        "::1",
        "169.254.169.254",  # the cloud metadata address
        "169.254.0.1",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "0.0.0.0",
        "::",
        "::ffff:127.0.0.1",  # a loopback address in IPv6 clothes
        "::ffff:169.254.169.254",
    ],
)
def test_always_refused(ip, cloud):
    with pytest.raises(AddressNotAllowed):
        check_host("peer.example.org", 443, policy(ip, cloud=cloud))


@pytest.mark.parametrize(
    "ip", ["10.0.0.5", "172.16.3.4", "192.168.1.9", "100.64.0.1", "100.127.255.254", "fc00::1", "fd12:3456::1"]
)
def test_private_ranges_are_refused_on_a_cloud_relay_and_allowed_at_home(ip):
    with pytest.raises(AddressNotAllowed):
        check_host("peer.example.org", 443, policy(ip, cloud=True))
    assert check_host("peer.example.org", 443, policy(ip, cloud=False)) == [ip]


@pytest.mark.parametrize("ip", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_addresses_pass_everywhere(ip):
    for cloud in (True, False):
        assert check_host("peer.example.org", 443, policy(ip, cloud=cloud)) == [ip]


def test_any_bad_address_refuses_the_whole_name():
    with pytest.raises(AddressNotAllowed):
        check_host("peer.example.org", 443, policy("93.184.216.34", "10.0.0.1", cloud=True))


def test_an_ip_literal_is_checked_too():
    for host in ("127.0.0.1", "[::1]", "169.254.169.254"):
        with pytest.raises(AddressNotAllowed):
            check_host(host, 443, AddressPolicy(cloud=False))
    assert check_host("93.184.216.34", 443, AddressPolicy(cloud=True)) == ["93.184.216.34"]


def test_loopback_only_through_the_explicit_injection():
    assert check_host("x", 443, policy("127.0.0.1", cloud=False, loopback=True)) == ["127.0.0.1"]
    # ... and the injection allows nothing else
    with pytest.raises(AddressNotAllowed):
        check_host("x", 443, policy("169.254.169.254", cloud=False, loopback=True))


def test_a_name_that_does_not_resolve_is_refused():
    def nothing(host: str, port: int) -> list[str]:
        raise AddressNotAllowed("no such host")

    with pytest.raises(AddressNotAllowed):
        check_host("nope.invalid", 443, AddressPolicy(cloud=False, resolver=nothing))
    with pytest.raises(AddressNotAllowed):
        check_host("x", 443, policy(cloud=False))


def test_localhost_by_name_is_loopback():
    with pytest.raises(AddressNotAllowed):
        check_host("localhost", 443, AddressPolicy(cloud=False))


def test_cloud_is_decided_from_auth_backend(monkeypatch):
    monkeypatch.setenv("AUTH_BACKEND", "firebase")
    assert AddressPolicy().is_cloud() is True
    monkeypatch.setenv("AUTH_BACKEND", "home")
    assert AddressPolicy().is_cloud() is False
    assert addr.AddressPolicy(cloud=True).is_cloud() is True


def test_there_is_no_environment_switch_for_loopback(monkeypatch):
    for name in ("FED_ALLOW_LOOPBACK", "FEDERATION", "FED_ALLOW_PRIVATE"):
        monkeypatch.setenv(name, "1")
    with pytest.raises(AddressNotAllowed):
        check_host("x", 443, AddressPolicy(cloud=False, resolver=lambda h, p: ["127.0.0.1"]))
