"""Broker credential management: `BrokerAdmin` over EMQX's REST API or Mosquitto dynamic-security.
Both apply the ACL of docs/PROTOCOL.md §2/§13.1 and report `"ok" | "manual" | "error"`."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

from app.config import Settings

if TYPE_CHECKING:
    from app.broker_mqtt import MqttBrokerClient

logger = logging.getLogger("relay.emqx_admin")

REQUEST_TIMEOUT_S = 5.0

# EMQX 5 built_in_database authentication mechanism id, fixed by `relay/emqx/emqx.conf`.
_AUTHN_MECHANISM = "password_based:built_in_database"

EmqxResult = Literal["ok", "manual", "error"]


class EmqxAdmin:
    """REST-only client for EMQX's authentication/authorization management API."""

    def __init__(self, settings: Settings, *, manages_auth: bool | None = None) -> None:
        self._base_url = settings.broker_api_url.rstrip("/")
        self._auth = (
            (settings.broker_api_key, settings.broker_api_secret or "")
            if settings.broker_api_key
            else None
        )
        self._manages_auth = _broker_manages_auth() if manages_auth is None else manages_auth


    def ensure_device(self, username: str, password: str, device_id: str) -> EmqxResult:
        """Creates/updates the device credential and its publish/subscribe ACL."""
        if not self._manages_auth:
            return "manual"
        return self._ensure_user_and_acl(username, password, _device_rules(device_id))

    def ensure_boot_user(self, bid: str, password: str) -> EmqxResult:
        """Creates/updates the bootstrap credential `boot-{bid}` and its ACL."""
        if not self._manages_auth:
            return "manual"
        username = _boot_username(bid)
        return self._ensure_user_and_acl(username, password, _boot_rules(bid))

    def delete_user(self, username: str) -> EmqxResult:
        """Removes the authentication user and ACL rule list; 404 counts as success."""
        if not self._manages_auth:
            return "manual"
        user_ok = self._delete(f"{self._authn_users_url()}/{username}")
        rule_ok = self._delete(f"{self._authz_users_url()}/{username}")
        return "ok" if user_ok and rule_ok else "error"


    def _authn_users_url(self) -> str:
        return f"{self._base_url}/authentication/{_AUTHN_MECHANISM}/users"

    def _authz_users_url(self) -> str:
        return f"{self._base_url}/authorization/sources/built_in_database/rules/users"

    def _request(self, method: str, url: str, json_body: dict[str, Any] | None) -> httpx.Response | None:
        try:
            return httpx.request(
                method, url, json=json_body, auth=self._auth, timeout=REQUEST_TIMEOUT_S
            )
        except httpx.HTTPError:
            logger.warning("emqx_admin %s %s failed", method, url, exc_info=True)
            return None

    def _delete(self, url: str) -> bool:
        resp = self._request("DELETE", url, None)
        if resp is None:
            return False
        # 404 = already gone, which is the caller's desired end state.
        if resp.status_code == 404 or 200 <= resp.status_code < 300:
            return True
        logger.warning("emqx_admin DELETE %s rejected: %s %r", url, resp.status_code, resp.text[:200])
        return False

    def _ensure_user_and_acl(
        self, username: str, password: str, rules: list[dict[str, str]]
    ) -> EmqxResult:
        if not self._ensure_authn_user(username, password):
            return "error"
        if not self._put_acl(username, rules):
            return "error"
        return "ok"

    def _ensure_authn_user(self, username: str, password: str) -> bool:
        create = self._request(
            "POST",
            self._authn_users_url(),
            {"user_id": username, "password": password, "is_superuser": False},
        )
        if create is not None and create.status_code in (200, 201):
            return True
        if create is not None and create.status_code == 409:
            update = self._request(
                "PUT",
                f"{self._authn_users_url()}/{username}",
                {"password": password, "is_superuser": False},
            )
            if update is not None and 200 <= update.status_code < 300:
                return True
            logger.warning(
                "emqx_admin: update user %s failed: %s",
                username,
                update.status_code if update is not None else "request error",
            )
            return False
        logger.warning(
            "emqx_admin: create user %s failed: %s",
            username,
            create.status_code if create is not None else "request error",
        )
        return False

    def _put_acl(self, username: str, rules: list[dict[str, str]]) -> bool:
        resp = self._request(
            "PUT",
            f"{self._authz_users_url()}/{username}",
            {"username": username, "rules": rules},
        )
        if resp is not None and 200 <= resp.status_code < 300:
            return True
        logger.warning(
            "emqx_admin: ACL rule push for %s failed: %s",
            username,
            resp.status_code if resp is not None else "request error",
        )
        return False


def _broker_manages_auth() -> bool:
    # Default on; "0" opts out for a deployment that manages EMQX auth some other way.
    return os.environ.get("BROKER_MANAGES_AUTH", "1") != "0"


def _boot_username(bid: str) -> str:
    return f"boot-{bid}"


def _device_rules(device_id: str) -> list[dict[str, str]]:
    return [
        {"topic": f"pager/{device_id}/up", "permission": "allow", "action": "publish"},
        {"topic": f"pager/{device_id}/status", "permission": "allow", "action": "publish"},
        {"topic": f"pager/{device_id}/loc", "permission": "allow", "action": "publish"},
        {"topic": f"pager/{device_id}/down", "permission": "allow", "action": "subscribe"},
    ]


def _boot_rules(bid: str) -> list[dict[str, str]]:
    return [
        {"topic": f"pager/boot/{bid}/up", "permission": "allow", "action": "publish"},
        {"topic": f"pager/boot/{bid}/down", "permission": "allow", "action": "subscribe"},
    ]


_dynsec_logger = logging.getLogger("relay.broker_admin")
_DYNSEC_GONE = ("Client not found", "Role not found")


class MosquittoDynsecAdmin:
    """`BrokerAdmin` over Mosquitto's dynamic-security control topic."""

    def __init__(self, broker: MqttBrokerClient, *, manages_auth: bool | None = None) -> None:
        self._broker = broker
        self._manages_auth = _broker_manages_auth() if manages_auth is None else manages_auth

    def ensure_device(self, username: str, password: str, device_id: str) -> EmqxResult:
        if not self._manages_auth:
            return "manual"
        return self._ensure(username, password, _dynsec_acls(_device_rules(device_id)))

    def ensure_boot_user(self, bid: str, password: str) -> EmqxResult:
        if not self._manages_auth:
            return "manual"
        return self._ensure(_boot_username(bid), password, _dynsec_acls(_boot_rules(bid)))

    def delete_user(self, username: str) -> EmqxResult:
        if not self._manages_auth:
            return "manual"
        responses = self._broker.control(
            [
                {"command": "deleteClient", "username": username},
                {"command": "deleteRole", "rolename": username},
            ]
        )
        if responses is None:
            return "error"
        ok = all(r.get("error") in (None, *_DYNSEC_GONE) for r in responses)
        if not ok:
            _dynsec_logger.warning("dynsec: delete %s failed: %r", username, responses)
        return "ok" if ok else "error"

    def _ensure(self, username: str, password: str, acls: list[dict[str, Any]]) -> EmqxResult:
        roles = [{"rolename": username}]
        first = self._broker.control(
            [
                {"command": "createRole", "rolename": username, "acls": acls},
                {"command": "createClient", "username": username, "password": password, "roles": roles},
            ]
        )
        if first is None:
            return "error"
        role, client = first
        retry: list[dict[str, Any]] = []
        # modify* rather than addRoleACL/addClientRole: Mosquitto 2.1.2 answers the latter with
        # "Internal error" when the client already holds the role.
        if role.get("error") == "Role already exists":
            retry.append({"command": "modifyRole", "rolename": username, "acls": acls})
        elif role.get("error"):
            _dynsec_logger.warning("dynsec: createRole %s failed: %s", username, role["error"])
            return "error"
        if client.get("error") == "Client already exists":
            retry.append(
                {"command": "modifyClient", "username": username, "password": password, "roles": roles}
            )
        elif client.get("error"):
            _dynsec_logger.warning("dynsec: createClient %s failed: %s", username, client["error"])
            return "error"
        if not retry:
            return "ok"
        second = self._broker.control(retry)
        if second is None:
            return "error"
        errors = [r["error"] for r in second if r.get("error")]
        if errors:
            _dynsec_logger.warning("dynsec: update %s failed: %s", username, errors)
            return "error"
        return "ok"


def _dynsec_acls(rules: list[dict[str, str]]) -> list[dict[str, Any]]:
    acls: list[dict[str, Any]] = []
    for rule in rules:
        allow = rule["permission"] == "allow"
        if rule["action"] == "publish":
            acls.append({"acltype": "publishClientSend", "topic": rule["topic"], "allow": allow})
        else:
            acls.append({"acltype": "subscribeLiteral", "topic": rule["topic"], "allow": allow})
            acls.append({"acltype": "publishClientReceive", "topic": rule["topic"], "allow": allow})
    return acls


class BrokerAdmin(Protocol):
    def ensure_device(self, username: str, password: str, device_id: str) -> EmqxResult: ...

    def ensure_boot_user(self, bid: str, password: str) -> EmqxResult: ...

    def delete_user(self, username: str) -> EmqxResult: ...


def make_broker_admin(settings: Settings, broker: Any) -> BrokerAdmin:
    if settings.broker_mode == "mqtt":
        return MosquittoDynsecAdmin(broker)
    return EmqxAdmin(settings)
