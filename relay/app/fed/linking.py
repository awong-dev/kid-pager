"""Linking two families: the issuer's `kpfed2.` invite and link request handling, and the acceptor's
`accept_invite`. Every refusal that does not depend on the code being right is the one 403."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import alerts as alerts_module
from app import wire
from app.config import Settings
from app.fed import client as client_mod
from app.fed import keys, moved, sign
from app.fed import sender as fed_sender
from app.fed.client import FedClient
from app.store import families as families_store
from app.store import fed_families, fed_links, fed_state
from app.store import rate_limits as rate_limits_store
from app.store.families import get_family

PREFIX = "kpfed2."
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
LINK_PATH = f"{client_mod.API}/link"
NAME_MAX = 60
LINK_RATE_LIMIT = 10
LINK_RATE_WINDOW_S = 60


class ReauthConfirm(Exception):
    """A `re` invite for an existing link: nothing changes until the admin confirms
    (`accept_invite(..., confirm_reauth=True)`). The route answers 409 `reauth_confirm`."""

    def __init__(self, link: fed_links.FedLink, blob: InviteBlob) -> None:
        super().__init__("reauth_confirm")
        self.link = link
        self.blob = blob


class LinkingError(Exception):
    """`status` is the HTTP status the admin route (or the other relay) should see; for a 502
    `detail` is one word of the fixed vocabulary (`client.ERROR_CODES`)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def clean_name(value: str) -> str:
    return " ".join(wire.CONTROL_CHAR_RE.sub(" ", value).split())[:NAME_MAX]


class _Coordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = Field(ge=1, le=65535)
    ca: str | None = None

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
            client_mod.tls_context(v)  # a PEM with a certificate in it, or ValueError
        return v or None


class _Party(_Coordinates):
    fedId: str
    pk: str
    name: str
    epoch: int = Field(default=0, ge=0)

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

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = clean_name(v)
        if not v:
            raise ValueError("empty name")
        return v


class InviteBlob(_Coordinates):
    fid: str
    pk: str
    name: str
    code: str
    # `re: 1` marks a reauthorization invite (the restored family's new key); `epoch` is that
    # key's epoch, so the confirming side can refuse a stale one before sending anything.
    re: int | None = None
    epoch: int | None = Field(default=None, ge=0)

    @field_validator("fid")
    @classmethod
    def _fid(cls, v: str) -> str:
        if not keys.FED_ID_RE.match(v):
            raise ValueError("not a federation id")
        return v

    @field_validator("pk")
    @classmethod
    def _pk(cls, v: str) -> str:
        keys.public_from_b64(v)
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return clean_name(v) or "Family"

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        if not CODE_RE.match(v):
            raise ValueError("bad code")
        return v


class LinkRequest(BaseModel):
    """The body of `POST /fed/v1/link`: the code, the issuer's fedId and the acceptor's own
    description (`from`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    code: str
    to: str
    from_: _Party = Field(alias="from")
    re: int = 0

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        if not CODE_RE.match(v):
            raise ValueError("bad code")
        return v


def encode_invite(blob: InviteBlob) -> str:
    raw = json.dumps(blob.model_dump(exclude_none=True), separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_invite(text: str) -> InviteBlob:
    """`ValueError` for anything that is not a well-formed `kpfed2.` blob."""
    text = text.strip()
    if not text.startswith(PREFIX):
        raise ValueError("not a link invite")
    payload = text[len(PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        return InviteBlob.model_validate_json(raw)
    except (ValueError, ValidationError) as exc:
        raise ValueError("malformed link invite") from exc


def public_host_port(settings: Settings) -> tuple[str, int]:
    """Where other relays dial this one: the host and port of `PUBLIC_BASE_URL` (443 without a
    port). `LinkingError(503)` when it is not set."""
    from urllib.parse import urlsplit

    parts = urlsplit(settings.public_base_url or "")
    host = (parts.hostname or "").lower()
    if not host:
        raise LinkingError(503, "PUBLIC_BASE_URL must be set to federate")
    return host, parts.port or 443


def own_ca(settings: Settings) -> str | None:
    return settings.fed_ca_pem or None


def family_name(family_id: str) -> str:
    family = get_family(family_id)
    return clean_name(family.name if family else family_id) or family_id


def _party(settings: Settings, identity: fed_families.FedFamily) -> dict:
    host, port = public_host_port(settings)
    out = {
        "fedId": identity.fedId,
        "pk": identity.publicKey,
        "name": family_name(identity.familyId),
        "host": host,
        "port": port,
        "epoch": identity.epoch,
    }
    ca = own_ca(settings)
    if ca:
        out["ca"] = ca
    return out


def new_invite(
    settings: Settings, identity: fed_families.FedFamily, created_by: str
) -> tuple[str, int]:
    """`(blob, expiresAt)`."""
    host, port = public_host_port(settings)
    code = base64.urlsafe_b64encode(os.urandom(16)).decode("ascii").rstrip("=")
    expires = fed_links.create_invite(code, identity.familyId, created_by)
    blob = InviteBlob(
        host=host,
        port=port,
        ca=own_ca(settings),
        fid=identity.fedId,
        pk=identity.publicKey,
        name=family_name(identity.familyId),
        code=code,
    )
    return encode_invite(blob), expires


def new_reauth_invite(
    settings: Settings,
    identity: fed_families.FedFamily,
    link: fed_links.FedLink,
    created_by: str,
    client: FedClient | None = None,
) -> tuple[str, str | None, int]:
    """`(invite, url, expiresAt)` for a restored family's `reauth` link. The blob is a normal
    `kpfed2.` one plus `re: 1`, carrying the family's unchanged fedId and its NEW key, bound to this
    link. `url` is `<their web base>/family/people#fedInvite=<invite>` when the other relay's
    `/fed/v1/info` reports a web base, else None: the invite rides in the URL fragment, which a
    browser never sends to any server."""
    host, port = public_host_port(settings)
    code = base64.urlsafe_b64encode(os.urandom(16)).decode("ascii").rstrip("=")
    expires = fed_links.create_invite(code, identity.familyId, created_by, link_id=link.id)
    blob = InviteBlob(
        host=host,
        port=port,
        ca=own_ca(settings),
        fid=identity.fedId,
        pk=identity.publicKey,
        name=family_name(identity.familyId),
        code=code,
        re=1,
        epoch=identity.epoch,
    )
    invite = encode_invite(blob)
    return invite, _web_url(client or client_mod.get_client(), link, invite), expires


def _web_url(client: FedClient, link: fed_links.FedLink, invite: str) -> str | None:
    try:
        reply = client.call(
            "GET",
            f"{client_mod.API}/info",
            host=link.remote.host,
            port=link.remote.port,
            tls_ca=link.remote.tlsCa,
        )
        web = reply.json().get("web") if reply.status == 200 else None
    except (client_mod.FedError, ValueError, AttributeError):
        return None
    if not isinstance(web, str) or not web.startswith("https://"):
        return None
    return f"{web.rstrip('/')}/family/people#fedInvite={invite}"


@dataclass(frozen=True, slots=True)
class LinkReply:
    body: bytes
    headers: dict[str, str]


def handle_link_request(
    settings: Settings,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    source: str,
) -> LinkReply:
    """`POST /fed/v1/link` on the issuer. Raises `LinkingError`: 429 over the source's rate limit,
    400 malformed, 401 bad signature or skew, 403 for everything that says "not for you" (unknown
    or switched-off family, bad / used / expired / foreign code), 409 already linked, over the
    link limit."""
    if not rate_limits_store.check_and_increment(
        f"fedlink:{source}",
        limit=LINK_RATE_LIMIT,
        window_s=LINK_RATE_WINDOW_S,
        now=time.time(),
    ):
        raise LinkingError(429, "rate limit")
    try:
        parsed_headers = sign.parse_headers(headers)
        req = LinkRequest.model_validate_json(body)
    except sign.SignatureError as exc:
        raise LinkingError(401, f"signature {exc.code}") from exc
    except ValidationError as exc:
        raise LinkingError(400, "malformed link request") from exc

    if parsed_headers.sender != req.from_.fedId or parsed_headers.receiver != req.to:
        raise LinkingError(400, "headers do not match the body")
    # Proof of possession: the acceptor signs with the key it claims in `from.pk`.
    try:
        sign.verify(keys.public_from_b64(req.from_.pk), parsed_headers, "POST", path, body)
    except sign.SignatureError as exc:
        raise LinkingError(401, f"signature {exc.code}") from exc

    identity = fed_families.get_by_fed_id(req.to)
    if (
        identity is None
        or not fed_state.family_enabled(identity.familyId)
        or req.from_.fedId == identity.fedId
    ):
        raise LinkingError(403, "forbidden")
    family_id = identity.familyId

    existing = fed_links.get_for(family_id, req.from_.fedId)
    if req.re:
        return _handle_reauth(settings, path, req, identity, existing)
    if existing is not None:
        if _is_repeat(req, existing):
            # The acceptor did not get our confirmation and is retrying: same code, same key,
            # before the invite's expiry. Answer it again.
            return _confirmation(settings, identity, path, req.from_.fedId)
        invite = fed_links.get_invite(req.code)
        if invite is None or invite.get("familyId") != family_id:
            raise LinkingError(403, "forbidden")  # a bad code learns nothing about links
        raise LinkingError(409, "already linked")
    invite = fed_links.get_invite(req.code)
    if invite is None or invite.get("familyId") != family_id:
        raise LinkingError(403, "forbidden")
    if len(fed_links.list_for_family(family_id)) >= fed_links.MAX_LINKS_PER_FAMILY:
        if invite.get("usedAt") is None and int(invite.get("expiresAt") or 0) >= time.time():
            raise LinkingError(409, "link limit reached")
        raise LinkingError(403, "forbidden")
    outcome = fed_links.consume_invite(
        req.code, family_id, used_by=req.from_.fedId, used_pk=req.from_.pk
    )
    if outcome != "ok":
        raise LinkingError(403, "forbidden")
    try:
        fed_links.create(
            family_id,
            remote=_remote_of(req.from_),
            created_by=invite.get("createdBy"),
        )
    except fed_links.LinkExists:
        raise LinkingError(409, "already linked") from None
    except fed_links.LimitReached:
        raise LinkingError(409, "link limit reached") from None
    return _confirmation(settings, identity, path, req.from_.fedId)


def _remote_of(party: _Party) -> fed_links.Remote:
    return fed_links.Remote(
        fedId=party.fedId,
        name=party.name,
        publicKey=party.pk,
        epoch=party.epoch,
        host=party.host,
        port=party.port,
        tlsCa=party.ca,
    )


def _handle_reauth(
    settings: Settings,
    path: str,
    req: LinkRequest,
    identity: fed_families.FedFamily,
    existing: fed_links.FedLink | None,
) -> LinkReply:
    """Handle a reauthorization on the restored relay. The signature was verified against `from.pk`.
    A link must exist for that family and be waiting for reauthorization, and the code must be the
    one issued for that link. When `from.pk` is the key the backup stored, the other family has
    proved it is the one this link was made with; otherwise the code alone vouches, and the link
    records `remoteKeyChanged`."""
    invite = fed_links.get_invite(req.code)
    if (
        existing is None
        or invite is None
        or invite.get("familyId") != identity.familyId
        or invite.get("linkId") != existing.id
    ):
        raise LinkingError(403, "forbidden")
    if existing.state != "reauth":
        # a repeat of the request that just succeeded (lost confirmation) is answered again
        if (
            existing.state == "active"
            and invite.get("usedBy") == req.from_.fedId
            and invite.get("usedPk") == req.from_.pk
            and existing.remote.publicKey == req.from_.pk
            and int(invite.get("expiresAt") or 0) >= time.time()
        ):
            return _confirmation(settings, identity, path, req.from_.fedId)
        raise LinkingError(403, "forbidden")
    outcome = fed_links.consume_invite(
        req.code,
        identity.familyId,
        used_by=req.from_.fedId,
        used_pk=req.from_.pk,
        link_id=existing.id,
    )
    if outcome != "ok":
        raise LinkingError(403, "forbidden")
    fed_links.apply_reauth(
        existing.id,
        _remote_of(req.from_),
        key_changed=req.from_.pk != existing.remote.publicKey,
    )
    return _confirmation(settings, identity, path, req.from_.fedId)


def _is_repeat(req: LinkRequest, existing: fed_links.FedLink) -> bool:
    """True when `req` repeats the request that spent its code: the invite was used by this
    family and key, has not expired, and the stored link has that key."""
    invite = fed_links.get_invite(req.code)
    if invite is None or invite.get("usedAt") is None:
        return False
    if int(invite.get("expiresAt") or 0) < time.time():
        return False
    return (
        invite.get("usedBy") == req.from_.fedId
        and invite.get("usedPk") == req.from_.pk
        and existing.remote.publicKey == req.from_.pk
    )


def _confirmation(
    settings: Settings,
    identity: fed_families.FedFamily,
    path: str,
    to_fed_id: str,
) -> LinkReply:
    reply = json.dumps(_party(settings, identity), separators=(",", ":")).encode()
    return LinkReply(
        body=reply,
        headers=sign.sign(
            keys.private_from_b64(identity.privateKey),
            identity.fedId,
            to_fed_id,
            sign.RESPONSE_METHOD,
            path,
            reply,
        ),
    )


def accept_invite(
    settings: Settings,
    identity: fed_families.FedFamily,
    invite: str,
    created_by: str,
    client: FedClient | None = None,
    *,
    confirm_reauth: bool = False,
) -> fed_links.FedLink:
    """Links `identity`'s family with the one that issued `invite`. `LinkingError`: 400 for a bad
    blob, 409 for self-linking, an existing link or the link limit, 502 (`detail` from the fixed
    vocabulary) when the other relay cannot be used or its confirmation does not verify."""
    try:
        blob = decode_invite(invite)
    except ValueError as exc:
        raise LinkingError(400, str(exc)) from exc
    if blob.fid == identity.fedId:
        raise LinkingError(409, "that invite is from this family")
    existing = fed_links.get_for(identity.familyId, blob.fid)
    if blob.re:
        # a reauthorization is never a way to make a new link, and never automatic.
        if existing is None:
            raise LinkingError(400, "no such link to reauthorize")
        if not confirm_reauth:
            raise ReauthConfirm(existing, blob)
        if blob.epoch is not None and blob.epoch <= existing.remote.epoch:
            raise LinkingError(400, "that invite is older than the link")
    elif existing is not None:
        raise LinkingError(409, "already linked")
    elif len(fed_links.list_for_family(identity.familyId)) >= fed_links.MAX_LINKS_PER_FAMILY:
        raise LinkingError(409, "link limit reached")

    client = client or client_mod.get_client()
    payload = json.dumps(
        {
            "code": blob.code,
            "to": blob.fid,
            "from": _party(settings, identity),
            **({"re": 1} if blob.re else {}),
        },
        separators=(",", ":"),
    ).encode()
    try:
        reply = client.call(
            "POST",
            LINK_PATH,
            host=blob.host,
            port=blob.port,
            tls_ca=blob.ca,
            body=payload,
            sender=identity,
            receiver=blob.fid,
        )
    except client_mod.FedError as exc:
        raise LinkingError(502, exc.code) from exc
    if reply.status == 409:
        raise LinkingError(409, "already linked")
    if reply.status != 200:
        raise LinkingError(502, "refused")

    # The confirmation must come from the key the invite named, for this acceptor.
    try:
        confirmation = sign.parse_headers(reply.headers)
        if confirmation.sender != blob.fid or confirmation.receiver != identity.fedId:
            raise sign.SignatureError("bad_sig")
        sign.verify(
            keys.public_from_b64(blob.pk),
            confirmation,
            sign.RESPONSE_METHOD,
            LINK_PATH,
            reply.body,
        )
        said = _Party.model_validate(reply.json())
        if said.fedId != blob.fid or said.pk != blob.pk:
            raise ValueError("identity mismatch")
    except (sign.SignatureError, ValueError, AttributeError, ValidationError) as exc:
        raise LinkingError(502, "bad_response") from exc

    remote = fed_links.Remote(
        fedId=blob.fid,
        name=said.name,
        publicKey=blob.pk,
        epoch=said.epoch,
        host=blob.host,
        port=blob.port,
        tlsCa=blob.ca,
    )
    if blob.re:
        assert existing is not None
        if said.epoch <= existing.remote.epoch:
            raise LinkingError(502, "bad_response")  # a stale key: nothing is replaced
        link = fed_links.apply_reauth(existing.id, remote, key_changed=False)
        alerts_module.fed_reauth(
            identity.familyId,
            link_id=link.id,
            name=remote.name,
            host=remote.host,
            epoch=remote.epoch,
        )
        return link
    try:
        return fed_links.create(
            identity.familyId,
            remote=remote,
            created_by=created_by,
        )
    except fed_links.LinkExists:
        raise LinkingError(409, "already linked") from None
    except fed_links.LimitReached:
        raise LinkingError(409, "link limit reached") from None


def delete_link(link: fed_links.FedLink, *, notify: bool) -> int:
    """Deletes `link` here: pending outbox rows fail `unpeered` (and the deliveries they stood
    for), the row goes, and the old key is let go once no other link waits for its announcement.
    The people of that family stay but are no longer sendable (`book.fed_link_reason`). With
    `notify` a best-effort signed `unlink` tells the other family (in the background; its failure
    is ignored: the other side sees 403s). Returns the rows failed."""
    identity = fed_families.get(link.familyId)
    failed = fed_sender.fail_link_rows(link.id, "unpeered")
    fed_links.delete(link.id)
    moved.settle_family(link.familyId)
    if notify and identity is not None:
        fed_sender.unlink_in_background(identity, link)
    return failed


def unlink_family(family_id: str) -> int:
    """A family is being purged: tell every linked family (best effort, in the background),
    unless it is frozen (a moved source says nothing).
    Its `fedFamilies`, `fedLinks`, invites and outbox rows are deleted by the purge itself.
    Returns the links told."""
    identity = fed_families.get(family_id)
    if identity is None:
        return 0
    links = fed_links.list_for_family(family_id)
    if families_store.is_frozen(family_id):
        # Exported for a move: its links now belong to the copy, and an `unlink` signed with the
        # still-valid old key would break them.
        return 0
    for link in links:
        if link.state == "active":
            fed_sender.unlink_in_background(identity, link)
    return len(links)
