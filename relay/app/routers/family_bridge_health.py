"""`/api/family/bridges/{id}/carrier*`: carrier, balance, carrier log and top-ups.

Family admin (or super naming the family); a bridge of another family is a 404.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import topups
from app.auth import AuthedUser, Principal, principal_for, require_user
from app.broker import BrokerClient
from app.carriers import registry
from app.carriers import service as carriers_service
from app.routers.family import FamilyScope, require_family_write_rate_limit
from app.store import bridges as bridges_store
from app.store import carrier_log
from app.store import rate_limits as rate_limits_store
from app.store import topups as topups_store

logger = logging.getLogger("relay.family_bridge_health")

router = APIRouter(prefix="/api/family")


def get_broker(request: Request) -> BrokerClient:
    return request.app.state.broker


def _bridge_or_404(bridge_id: str, family_id: str) -> bridges_store.Bridge:
    bridge = bridges_store.get(bridge_id)
    if bridge is None or bridge.familyId != family_id:
        raise HTTPException(status_code=404, detail="no such bridge")
    return bridge


class CarrierChoice(BaseModel):
    id: str
    name: str
    canCheck: bool
    canTopup: bool


class LogRow(BaseModel):
    id: str
    at: datetime | None
    # `in` (the carrier's reply) or `out` (what the relay sent it)
    dir: str
    party: str
    text: str
    # replies: balance, topup_ok, topup_failed, low, unknown; commands: check, topup, ack
    kind: str
    detail: str | None


class CarrierOut(BaseModel):
    carrier: carriers_service.CarrierView
    # Every carrier an admin may pick.
    carriers: list[CarrierChoice]
    # Newest first, at most 50.
    log: list[LogRow]


def _out(bridge: bridges_store.Bridge) -> CarrierOut:
    return CarrierOut(
        carrier=carriers_service.view(bridge),
        carriers=[
            CarrierChoice(
                id=c.id,
                name=c.name,
                canCheck=c.balance_command() is not None,
                canTopup=bool(c.topup_packs()),
            )
            for c in registry.all_carriers()
        ],
        log=[LogRow(**r.model_dump()) for r in carrier_log.list_recent(bridge.id)],
    )


def _fresh(bridge_id: str) -> bridges_store.Bridge:
    fresh = bridges_store.get(bridge_id)
    assert fresh is not None
    return fresh


@router.get("/bridges/{bridge_id}/carrier")
def get_carrier(bridge_id: str, scope: FamilyScope) -> CarrierOut:
    _, family_id = scope
    return _out(_bridge_or_404(bridge_id, family_id))


class SetCarrierRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A carrier id from `carriers`, or null to go back to auto-matching the SIM.
    id: str | None


@router.put("/bridges/{bridge_id}/carrier", dependencies=[Depends(require_family_write_rate_limit)])
def set_carrier(bridge_id: str, req: SetCarrierRequest, scope: FamilyScope) -> CarrierOut:
    """Manual carrier override: an MVNO SIM often reports its host network."""
    _, family_id = scope
    bridge = _bridge_or_404(bridge_id, family_id)
    if req.id is not None and not registry.known(req.id):
        raise HTTPException(status_code=422, detail="unknown carrier")
    carriers_service.set_carrier(bridge, req.id)
    return _out(_fresh(bridge.id))


class SetBalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Texts left now; omit with `unlimited: true` for an unlimited plan.
    texts: int | None = Field(default=None, ge=0, le=1_000_000)
    unlimited: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> SetBalanceRequest:
        if self.unlimited == (self.texts is not None):
            raise ValueError("send either texts or unlimited: true")
        return self


@router.put(
    "/bridges/{bridge_id}/carrier/balance", dependencies=[Depends(require_family_write_rate_limit)]
)
def set_balance(
    bridge_id: str,
    req: SetBalanceRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> CarrierOut:
    """The admin types the allowance (`source: manual`); the relay subtracts what is sent."""
    _, family_id = scope
    bridge = _bridge_or_404(bridge_id, family_id)
    carriers_service.set_balance(bridge, None if req.unlimited else req.texts, broker=broker)
    return _out(_fresh(bridge.id))


@router.post(
    "/bridges/{bridge_id}/carrier/check",
    status_code=202,
    dependencies=[Depends(require_family_write_rate_limit)],
)
def check_now(bridge_id: str, scope: FamilyScope) -> dict[str, str]:
    """Ask the carrier for the balance now: 202 `{outboxId}`. 409 when the carrier has no
    balance command or the phone cannot send; 429 inside five minutes of the last check."""
    _, family_id = scope
    bridge = _bridge_or_404(bridge_id, family_id)
    try:
        item = carriers_service.enqueue_check(bridge, manual=True)
    except carriers_service.CheckRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    assert item is not None
    return {"outboxId": item.id}


TOPUP_PRESSES_PER_HOUR = 20


def topup_scope(
    bridge_id: str,
    authed: Annotated[AuthedUser, Depends(require_user)],
    family: Annotated[str | None, Query()] = None,
    x_family: Annotated[str | None, Header(alias="X-Family")] = None,
) -> tuple[Principal, bridges_store.Bridge]:
    """Family admin of the bridge's family (or super) or its owner; 404 other family, 403 member."""
    principal = principal_for(authed)
    requested = family or x_family
    bridge = bridges_store.get(bridge_id)
    if principal.role == "super":
        if not requested:
            raise HTTPException(status_code=400, detail="?family= or X-Family is required")
        if bridge is None or bridge.familyId != requested:
            raise HTTPException(status_code=404, detail="no such bridge")
        return principal, bridge
    if principal.family_id is None or bridge is None or bridge.familyId != principal.family_id:
        raise HTTPException(status_code=404, detail="no such bridge")
    if requested is not None and requested != principal.family_id:
        raise HTTPException(status_code=403, detail="cannot act on another family")
    if principal.role == "admin" or bridge.ownerUid == principal.uid:
        return principal, bridge
    raise HTTPException(status_code=403, detail="family admin or the bridge's owner only")


TopupScope = Annotated[tuple[Principal, bridges_store.Bridge], Depends(topup_scope)]


class TopupOut(BaseModel):
    id: str
    pack: str
    packLabel: str
    texts: int
    by: str
    at: datetime
    # requested | confirmed | failed | unconfirmed (failed does not count against the limit)
    state: str
    note: str | None


class TopupsOut(BaseModel):
    # off | limited | unlimited, and the per-month limit when limited
    mode: str
    perMonth: int | None
    # Counted top-ups this calendar month (family zone), failed ones excluded.
    used: int
    # May the caller press it now; otherwise `reason` says why not.
    allowed: bool
    reason: str | None
    packs: list[dict]
    # Newest first.
    history: list[TopupOut]


class RequestTopupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack: str = Field(min_length=1, max_length=40)


def _topups_out(bridge: bridges_store.Bridge) -> TopupsOut:
    allowance = topups.allowance(bridge)
    return TopupsOut(
        **allowance.model_dump(),
        history=[TopupOut(**r.model_dump(include=set(TopupOut.model_fields))) for r in
                 topups_store.list_recent(bridge.id, limit=50)],
    )


@router.get("/bridges/{bridge_id}/topups")
def get_topups(scope: TopupScope) -> TopupsOut:
    """The policy, this month's count, whether the button may be pressed (and why not) and
    the history."""
    _, bridge = scope
    return _topups_out(bridge)


@router.post("/bridges/{bridge_id}/topup", status_code=201)
def request_topup(req: RequestTopupRequest, scope: TopupScope) -> TopupOut:
    """Press "Top up": re-checks the family policy in the same transaction that writes the
    `requested` row, then queues the carrier's command. 403 when top-ups are off or the month's
    limit is reached; 409 while another is waiting for its reply or the phone cannot send;
    404 for an unknown pack."""
    principal, bridge = scope
    if not rate_limits_store.check_and_increment(
        f"topup:{principal.uid}", limit=TOPUP_PRESSES_PER_HOUR, window_s=3600
    ):
        raise HTTPException(status_code=429, detail="too many top-up requests; try again later")
    try:
        doc = topups.request(bridge, req.pack, principal.uid)
    except topups.Refused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return TopupOut(**doc.model_dump(include=set(TopupOut.model_fields)))
