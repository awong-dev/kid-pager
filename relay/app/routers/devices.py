"""`/api/devices/*`: the owner-facing device surface (list, SMS contacts, GNSS, SMS log)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import devcfg
from app.auth import AuthedUser, principal_for, require_user
from app.broker import BrokerClient
from app.store import devices as devices_store
from app.store import sms as sms_store
from app.store.devices import Device, DeviceStatus, SmsContact

logger = logging.getLogger("relay.devices")

router = APIRouter(prefix="/api/devices")


def get_broker(request: Request) -> BrokerClient:
    """The app's broker client."""
    return request.app.state.broker


def _is_family_admin_or_super(device: Device, authed: AuthedUser) -> bool:
    """Owner-or-admin check: family admin of the device's family, or super. Claims only."""
    principal = principal_for(authed)
    if principal.role == "super":
        return True
    return principal.role == "admin" and principal.family_id == device.familyId


def _require_owner_or_admin(device_id: str, authed: AuthedUser) -> Device:
    """404 for an unknown device, 403 for a caller who is not its owner, family admin or super."""
    device = devices_store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="no such device")
    if device.ownerUid != authed.uid and not _is_family_admin_or_super(device, authed):
        raise HTTPException(status_code=403, detail="not this device's owner")
    return device


class DeviceSummary(BaseModel):
    id: str
    label: str
    status: DeviceStatus


@router.get("")
def list_my_devices(authed: Annotated[AuthedUser, Depends(require_user)]) -> list[DeviceSummary]:
    return [
        DeviceSummary(id=d.id, label=d.label, status=d.status)
        for d in devices_store.list_devices(owner_uid=authed.uid)
    ]


class SmsContactsResponse(BaseModel):
    contacts: list[SmsContact]
    # True while a `cfg.sms` push is pending or unacked at the device.
    pending: bool


@router.get("/{device_id}/sms-contacts")
def get_sms_contacts(
    device_id: str, authed: Annotated[AuthedUser, Depends(require_user)]
) -> SmsContactsResponse:
    device = _require_owner_or_admin(device_id, authed)
    return SmsContactsResponse(
        contacts=device.smsContacts, pending=devcfg.sms_pending(device_id)
    )


@router.put("/{device_id}/sms-contacts")
def put_sms_contacts(
    device_id: str, authed: Annotated[AuthedUser, Depends(require_user)]
) -> None:
    """Retired: contacts are a projection of the member's approved numbers; kept for the error."""
    raise HTTPException(
        status_code=405, detail="Managed from People → Approved numbers"
    )


class GnssConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    en: bool


class GnssConfigResponse(BaseModel):
    en: bool
    pending: bool
    # Last `/status` `gnss` (0/1); None = never reported / older firmware.
    reported: int | None = None


def _gnss_response(device_id: str, device: Device) -> GnssConfigResponse:
    return GnssConfigResponse(
        en=devices_store.get_gnss_enabled(device_id),
        pending=devcfg.loc_pending(device_id),
        reported=device.status.gnss,
    )


@router.get("/{device_id}/gnss")
def get_gnss(
    device_id: str, authed: Annotated[AuthedUser, Depends(require_user)]
) -> GnssConfigResponse:
    device = _require_owner_or_admin(device_id, authed)
    return _gnss_response(device_id, device)


@router.put("/{device_id}/gnss")
def put_gnss(
    device_id: str,
    req: GnssConfig,
    authed: Annotated[AuthedUser, Depends(require_user)],
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> GnssConfigResponse:
    device = _require_owner_or_admin(device_id, authed)
    devices_store.set_gnss_enabled(device_id, req.en)
    devcfg.push_loc(device_id, gnss=req.en, broker=broker)
    return _gnss_response(device_id, device)


class SmsLogEntryOut(BaseModel):
    id: str
    ts: int
    smsTs: int
    dir: str | None
    peer: str | None
    name: str | None
    st: str | None
    body: str | None
    # An audit row of an sms_log upload that could not be stored normally.
    malformed: bool = False
    reason: str | None = None
    rawHex: str | None = None


class SmsLogResponse(BaseModel):
    entries: list[SmsLogEntryOut]


@router.get("/{device_id}/sms-log")
def get_sms_log(
    device_id: str,
    authed: Annotated[AuthedUser, Depends(require_user)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before: int | None = None,
) -> SmsLogResponse:
    device = _require_owner_or_admin(device_id, authed)
    # `name` is resolved from the current contacts at read time, never stored on the log row.
    contacts_by_phone = {c.phone: c.name for c in device.smsContacts}
    entries = sms_store.list_log(device_id, limit=limit, before=before)
    return SmsLogResponse(
        entries=[
            SmsLogEntryOut(
                id=e.id,
                ts=e.ts,
                smsTs=e.smsTs,
                dir=e.dir,
                peer=e.peer,
                name=contacts_by_phone.get(e.peer),
                st=e.st,
                body=e.body,
                malformed=e.malformed,
                reason=e.reason,
                rawHex=e.rawHex,
            )
            for e in entries
        ]
    )
