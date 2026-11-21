"""The data map: every collection the relay touches, with its family scope, retention, export.
Fields: `family`, `retention`, `export`, `uid_fields`, `time_field`, `links` (see `Coll`)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

Family = Literal[
    "path",
    "field:familyId",
    "field:familyIds",
    "field:ownerFamilyId",
    "via:device",
    "via:user",
    "via:bridge",
    "global",
]
Retention = Literal[
    "content", "location", "telemetry", "alerts", "ephemeral", "cache", "owner"
]
Export = Literal["data", "history", "secret", "move", "skip"]


class Coll(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    parent: str | None = None  # parent collection name for a subcollection
    family: Family
    retention: Retention
    export: Export
    uid_fields: tuple[str, ...] = ()
    id_embeds_uid: bool = False
    time_field: str | None = None
    # `device:deviceId` (a field), `device:@id` (the doc id), `bridge:bridgeId`.
    links: tuple[str, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> Coll:
        if self.parent is not None and self.family not in (
            "path",
            "via:device",
            "via:user",
        ):
            raise ValueError(
                f"{self.name}: a subcollection is scoped through its parent"
            )
        return self


def _c(name: str, family: Family, retention: Retention, export: Export, **kw) -> Coll:
    return Coll(name=name, family=family, retention=retention, export=export, **kw)


REGISTRY: tuple[Coll, ...] = (
    _c(
        "users",
        "field:familyId",
        "owner",
        "data",
        uid_fields=("uid",),
        note="externals carry ownerFamilyId; super users have neither",
    ),
    _c("aliases", "via:user", "owner", "data", uid_fields=("uid",)),
    _c("smsNumbers", "via:user", "owner", "data", uid_fields=("uid",)),
    _c("credentials", "path", "owner", "secret", parent="users", note="passkeys"),
    _c(
        "backends",
        "path",
        "owner",
        "data",
        parent="users",
        note="transport links; link tokens inside",
    ),
    _c(
        "book", "path", "owner", "data", parent="users", note="the pager's contact book"
    ),
    _c(
        "pushSubscriptions",
        "path",
        "owner",
        "skip",
        parent="users",
        note="web-push endpoint + keys",
    ),
    _c("pushTokens", "path", "owner", "skip", parent="users", note="FCM tokens"),
    _c(
        "allow",
        "field:familyIds",
        "owner",
        "data",
        uid_fields=("fromUid", "toUid"),
        id_embeds_uid=True,
    ),
    _c(
        "contactNames",
        "field:familyId",
        "owner",
        "data",
        uid_fields=("uid",),
        note="doc id {familyId}_{digest}",
    ),
    _c(
        "families",
        "path",
        "owner",
        "data",
        note="doc id is the family id; `federation` (the super's per-family switch) travels with a move and is dropped from a --live backup",
    ),
    _c(
        "devices",
        "field:familyId",
        "owner",
        "data",
        uid_fields=("ownerUid", "defaultToUid"),
        note="status.* position fields are location class",
    ),
    _c(
        "deviceSecrets",
        "via:device",
        "owner",
        "secret",
        note="doc id is the device id",
        links=("device:@id",),
    ),
    _c(
        "locations",
        "via:device",
        "location",
        "history",
        parent="devices",
        time_field="createdAt",
    ),
    _c(
        "battery",
        "via:device",
        "telemetry",
        "history",
        parent="devices",
        time_field="createdAt",
    ),
    _c(
        "smsLog",
        "via:device",
        "content",
        "history",
        parent="devices",
        uid_fields=("peerUid",),
        time_field="receivedAt",
    ),
    _c(
        "locReqs",
        "via:device",
        "location",
        "skip",
        time_field="createdAt",
        note="doc id is the device id",
        links=("device:@id",),
    ),
    _c(
        "locWireIds",
        "via:device",
        "location",
        "skip",
        time_field="createdAt",
        note="dedup marker {deviceId}_{id}",
        links=("device:deviceId",),
    ),
    _c(
        "setupCodes",
        "via:device",
        "ephemeral",
        "skip",
        time_field="expiresAt",
        links=("device:deviceId",),
    ),
    _c(
        "messages",
        "field:familyIds",
        "content",
        "history",
        uid_fields=("uids", "senderUid", "recipientUid"),
        time_field="createdAt",
        note="kind:'loc' carries a loc field (location class, scrubbed in place)",
    ),
    _c(
        "wireIds",
        "via:user",
        "content",
        "history",
        id_embeds_uid=True,
        time_field="createdAt",
        note="dedup marker {wireId}_{recipientUid}",
    ),
    _c(
        "conversations",
        "field:familyIds",
        "content",
        "data",
        uid_fields=("uids",),
        time_field="lastMessageAt",
        note="participants map is keyed by uid",
    ),
    _c(
        "heldSms",
        "field:familyId",
        "content",
        "history",
        uid_fields=("toUid",),
        time_field="receivedAt",
    ),
    _c(
        "heldChat",
        "field:familyId",
        "content",
        "history",
        uid_fields=("toUid",),
        time_field="receivedAt",
    ),
    _c(
        "alerts",
        "path",
        "alerts",
        "data",
        parent="families",
        uid_fields=("subjectUid", "peerUid"),
        time_field="ts",
    ),
    _c(
        "contactRequests",
        "via:user",
        "ephemeral",
        "data",
        uid_fields=("ownerUid", "peerUid"),
        time_field="createdAt",
        note="90 days",
        links=("device:deviceId",),
    ),
    _c(
        "bridges",
        "field:familyId",
        "owner",
        "data",
        uid_fields=("ownerUid",),
        note="tokenHash (secret) kept, fcmToken dropped by the family export",
    ),
    _c(
        "outbox",
        "path",
        "content",
        "skip",
        parent="bridges",
        time_field="ackedAt",
        note="acked rows only",
    ),
    _c(
        "carrierLog",
        "path",
        "content",
        "skip",
        parent="bridges",
        time_field="at",
        note="raw carrier replies and the commands sent; last 50 per bridge, trimmed on write",
    ),
    _c(
        "topups",
        "path",
        "owner",
        "history",
        parent="bridges",
        time_field="at",
        note="top-up requests per bridge (`by` names the uid that pressed; no index, ids only); swept after 400 days",
    ),
    _c(
        "bridgeConversations",
        "field:familyId",
        "owner",
        "data",
        uid_fields=("ownerUid", "uid"),
        links=("bridge:bridgeId",),
    ),
    _c(
        "bridgePairCodes",
        "via:bridge",
        "ephemeral",
        "skip",
        time_field="expiresAt",
        links=("bridge:bridgeId",),
    ),
    _c(
        "gchatLinkCodes",
        "via:user",
        "ephemeral",
        "skip",
        uid_fields=("uid",),
        time_field="expiresAt",
    ),
    _c("gchatSpaces", "via:user", "owner", "skip", uid_fields=("uid",)),
    _c(
        "sessions",
        "via:user",
        "ephemeral",
        "skip",
        uid_fields=("uid",),
        time_field="lastSeenAt",
    ),
    _c(
        "inviteCodes",
        "via:user",
        "ephemeral",
        "skip",
        uid_fields=("uid", "createdBy"),
        time_field="expiresAt",
    ),
    _c(
        "webauthnChallenges",
        "via:user",
        "ephemeral",
        "skip",
        uid_fields=("uid",),
        time_field="expiresAt",
        note="uid may be null",
    ),
    _c(
        "rateLimits",
        "global",
        "ephemeral",
        "skip",
        time_field="windowStart",
        note="doc id embeds a client IP",
    ),
    _c("cells", "global", "cache", "skip", time_field="resolvedAt"),
    _c("cas", "global", "owner", "skip", note="public CA PEMs, content-addressed"),
    _c("batteryModels", "global", "owner", "skip", uid_fields=("updatedBy",)),
    _c(
        "settings",
        "global",
        "owner",
        "skip",
        note="docs: retention, meta, federation (the server switch)",
    ),
    _c(
        "fedFamilies",
        "field:familyId",
        "owner",
        "move",
        note="doc id is the family id; the family's Ed25519 key pair (private key included) and "
        "stable fedId; server-only. A move export carries it, the import rotates the key",
    ),
    _c(
        "fedLinks",
        "field:familyId",
        "owner",
        "move",
        note="doc id {familyId}_{remoteFedId}; the pinned remote key and address; shared = this "
        "family's member uids (not in uid_fields: a member's purge drops the entry, not the link)",
    ),
    _c(
        "fedInvites",
        "field:familyId",
        "ephemeral",
        "skip",
        time_field="expiresAt",
        note="doc id sha256(code); expiresAt is unix seconds",
    ),
    _c(
        "fedOutbox",
        "field:familyId",
        "content",
        "skip",
        time_field="ackedAt",
        note="events waiting for a linked family; acked and failed rows age out 7 days after "
        "ackedAt; pending rows do not travel with a move (send before you freeze)",
    ),
    _c(
        "hooks",
        "global",
        "owner",
        "skip",
        uid_fields=("createdBy",),
        note="message hooks: server-owner config with a secret, not family data",
    ),
)

BY_NAME: dict[str, Coll] = {c.name: c for c in REGISTRY}
assert len(BY_NAME) == len(REGISTRY), "duplicate collection name in datamap.REGISTRY"


def get(name: str) -> Coll:
    return BY_NAME[name]
