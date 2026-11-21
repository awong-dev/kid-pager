"""A family moved to another server: `announce` posts `moved` signed with the old key; `handle_moved`
applies it on the other end by `epoch`."""

from __future__ import annotations

import json
import logging
import time

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import alerts as alerts_module
from app.config import Settings
from app.fed import client as client_mod
from app.fed import keys
from app.fed.client import FedClient, FedError, FedHttpError
from app.store import fed_families, fed_links
from app.store.fed_links import FedLink

logger = logging.getLogger("relay.fed.moved")

MOVED_PATH = f"{client_mod.API}/moved"
BACKOFF_S = (5.0, 30.0, 120.0)
REFUSED_RETRY_S = 24 * 3600


class MovedError(Exception):
    """`status` for the route: 400 malformed, 403 a prior key for anything but a repeat,
    409 stale."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class MovedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fedId: str
    epoch: int = Field(ge=0)
    pk: str
    host: str
    port: int = Field(ge=1, le=65535)
    ca: str | None = None

    @field_validator("fedId")
    @classmethod
    def _fed_id(cls, v: str) -> str:
        if not keys.FED_ID_RE.match(v):
            raise ValueError("not a federation id")
        return v

    @field_validator("pk")
    @classmethod
    def _pk(cls, v: str) -> str:
        keys.public_from_b64(v)
        return v

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip().lower()
        if not fed_links.valid_host(v):
            raise ValueError("not a host name")
        return v

    @field_validator("ca")
    @classmethod
    def _ca(cls, v: str | None) -> str | None:
        if v:
            client_mod.tls_context(v)
        return v or None


def body_for(settings: Settings, identity: fed_families.FedFamily) -> bytes:
    from app.fed import linking

    host, port = linking.public_host_port(settings)
    out: dict = {
        "fedId": identity.fedId,
        "epoch": identity.epoch,
        "pk": identity.publicKey,
        "host": host,
        "port": port,
    }
    ca = linking.own_ca(settings)
    if ca:
        out["ca"] = ca
    return json.dumps(out, separators=(",", ":")).encode()


def announce(
    settings: Settings,
    client: FedClient,
    identity: fed_families.FedFamily,
    link: FedLink,
    *,
    now: float | None = None,
) -> bool:
    """Posts `moved` for `link` when it is due. True when the link is announced (now, or it
    needed none); False when it is not due yet or the attempt failed."""
    t = time.time() if now is None else now
    current = link.announce
    if current is None or not current.pending:
        return True
    if identity.prevPrivateKey is None:
        # Nothing to sign with: no key change to announce.
        fed_links.set_announce(link.id, None)
        _maybe_forget_prev_key(identity.familyId)
        return True
    if current.nextAt > t:
        return False
    try:
        reply = client.link_call(
            identity,
            link,
            "POST",
            MOVED_PATH,
            body_for(settings, identity),
            private_key_b64=identity.prevPrivateKey,
        )
        if reply.status != 200:
            raise FedHttpError(reply.status)
    except FedError as exc:
        attempts = current.attempts + 1
        refused = isinstance(exc, FedHttpError) and exc.status < 500
        if refused:
            wait = REFUSED_RETRY_S
        else:
            wait = BACKOFF_S[attempts - 1] if attempts <= len(BACKOFF_S) else 0.0
        fed_links.record_err(link.id)
        fed_links.set_announce(
            link.id,
            fed_links.Announce(
                pending=True, since=current.since, attempts=attempts, nextAt=t + wait
            ),
        )
        logger.info("fed moved not accepted link=%s attempt=%d err=%s", link.id, attempts, exc)
        return False
    fed_links.record_ok(link.id)
    fed_links.set_announce(link.id, None)
    _maybe_forget_prev_key(link.familyId)
    return True


def _maybe_forget_prev_key(family_id: str) -> None:
    """When no link of the family still waits for its announcement, the old key is dead weight."""
    if not fed_links.pending_announce(family_id):
        fed_families.clear_prev(family_id)


def settle_family(family_id: str) -> None:
    """After a link is deleted: its pending announcement no longer holds the old key."""
    _maybe_forget_prev_key(family_id)


def handle_moved(
    family_id: str, link: FedLink, raw: bytes, *, used_prev_key: bool
) -> FedLink:
    """Applies one `moved` from the family on the other end of `link`. `MovedError` when it is
    malformed (400), signed with a prior key for anything but a repeat (403), or stale (409)."""
    try:
        body = MovedBody.model_validate_json(raw)
    except ValidationError as exc:
        raise MovedError(400, "malformed moved") from exc
    if body.fedId != link.remote.fedId:
        raise MovedError(400, "fedId does not match the link")
    remote = link.remote
    same = (
        body.pk == remote.publicKey
        and body.host == remote.host
        and body.port == remote.port
        and (body.ca or None) == (remote.tlsCa or None)
    )
    if used_prev_key:
        # Only a repeat of the announcement already applied may use the key it replaced.
        if body.epoch == remote.epoch and same:
            return link
        raise MovedError(403, "forbidden")
    if body.epoch > remote.epoch:
        fed_links.update_remote(
            link.id,
            publicKey=body.pk,
            prevPublicKey=remote.publicKey,
            epoch=body.epoch,
            host=body.host,
            port=body.port,
            tlsCa=body.ca,
        )
        alerts_module.fed_moved(
            family_id,
            link_id=link.id,
            name=remote.name,
            old_host=remote.host,
            new_host=body.host,
            epoch=body.epoch,
        )
        updated = fed_links.get(link.id)
        assert updated is not None
        return updated
    if body.epoch == remote.epoch and same:
        return link  # a retry after a lost acknowledgement
    raise MovedError(409, "stale")
