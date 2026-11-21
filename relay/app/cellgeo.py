"""Pluggable cell-tower-to-position resolver (docs/PROTOCOL.md §13.2).
`CELL_GEO_PROVIDER` (`google`/`opencellid`/`none`) and `CELL_GEO_API_KEY` are read on every call."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from app.store import cells as cells_store
from app.wire import CellInfo

logger = logging.getLogger("relay.cellgeo")

REQUEST_TIMEOUT_S = 5.0

# A bad provider response must not plant a pin at (0,0) or show a 50 km+ radius fix.
MAX_ACCURACY_M = 50_000

_GOOGLE_URL = "https://www.googleapis.com/geolocation/v1/geolocate"
_OPENCELLID_URL = "https://opencellid.org/cell/get"


@dataclass(frozen=True, slots=True)
class CellFix:
    lat: float
    lon: float
    acc_m: int
    provider: str


def provider_name() -> str:
    return (os.environ.get("CELL_GEO_PROVIDER") or "none").strip().lower() or "none"


def api_key() -> str | None:
    return os.environ.get("CELL_GEO_API_KEY") or None


def _sane(fix: CellFix) -> bool:
    if fix.acc_m > MAX_ACCURACY_M:
        return False
    return not (fix.lat == 0.0 and fix.lon == 0.0)


def resolve(cell: CellInfo) -> CellFix | None:
    """`None` means no position available (disabled, unknown, failed, implausible); never raises."""
    name = provider_name()
    if name == "none":
        return None

    cached = cells_store.get_cached(cell.mcc, cell.mnc, cell.tac, cell.ci)
    if cached is not None:
        if cached.unknown:
            return None
        if cached.lat is not None and cached.lon is not None and cached.accM is not None:
            return CellFix(
                lat=cached.lat, lon=cached.lon, acc_m=cached.accM, provider=cached.provider or name
            )
        return None

    key = api_key()
    if not key:
        logger.warning(
            "CELL_GEO_PROVIDER=%s but CELL_GEO_API_KEY is unset; skipping cell lookup", name
        )
        return None

    try:
        if name == "google":
            fix = _resolve_google(cell, key)
        elif name == "opencellid":
            fix = _resolve_opencellid(cell, key)
        else:
            logger.warning("unknown CELL_GEO_PROVIDER=%r; skipping cell lookup", name)
            return None
    except Exception as exc:  # noqa: BLE001 -- must never break the /webhooks/mqtt ingest path
        logger.warning("cell geolocation lookup failed (provider=%s): %s", name, type(exc).__name__)
        return None

    if fix is None:
        cells_store.remember_unknown(cell.mcc, cell.mnc, cell.tac, cell.ci, provider=name)
        return None
    if not _sane(fix):
        logger.warning(
            "cell geolocation lookup returned an implausible fix (provider=%s, acc_m=%s); "
            "treated as unresolved",
            name,
            fix.acc_m,
        )
        cells_store.remember_unknown(cell.mcc, cell.mnc, cell.tac, cell.ci, provider=name)
        return None

    cells_store.remember_resolved(
        cell.mcc, cell.mnc, cell.tac, cell.ci, lat=fix.lat, lon=fix.lon, acc_m=fix.acc_m, provider=name
    )
    return fix


def _resolve_google(cell: CellInfo, key: str) -> CellFix | None:
    """`considerIp` must be `false`, else Google falls back to the relay's own IP location."""
    body: dict[str, object] = {
        "considerIp": False,
        "radioType": "lte",
        "cellTowers": [
            {
                "cellId": cell.ci,
                "locationAreaCode": cell.tac,
                "mobileCountryCode": int(cell.mcc),
                "mobileNetworkCode": int(cell.mnc),
                **({"signalStrength": cell.rsrp} if cell.rsrp is not None else {}),
            }
        ],
    }
    resp = httpx.post(_GOOGLE_URL, params={"key": key}, json=body, timeout=REQUEST_TIMEOUT_S)
    if resp.status_code == 404:
        return None  # "notFound": Google does not know this cell.
    if resp.status_code != 200:
        logger.warning("google geolocation returned status=%s", resp.status_code)
        return None
    data = resp.json()
    location = data.get("location") or {}
    lat, lon, acc = location.get("lat"), location.get("lng"), data.get("accuracy")
    if lat is None or lon is None or acc is None:
        return None
    return CellFix(lat=float(lat), lon=float(lon), acc_m=round(float(acc)), provider="google")


def _resolve_opencellid(cell: CellInfo, key: str) -> CellFix | None:
    """OpenCelliD `cell/get`; a body with no `lat`/`lon` (or an `"error"`) is an unknown cell."""
    resp = httpx.get(
        _OPENCELLID_URL,
        params={
            "key": key,
            "mcc": cell.mcc,
            "mnc": cell.mnc,
            "lac": cell.tac,
            "cellid": cell.ci,
            "format": "json",
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    if resp.status_code != 200:
        logger.warning("opencellid returned status=%s", resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("opencellid returned a non-JSON body")
        return None
    if not isinstance(data, dict) or "error" in data or "lat" not in data or "lon" not in data:
        return None  # unknown cell
    acc = data.get("range")
    return CellFix(
        lat=float(data["lat"]),
        lon=float(data["lon"]),
        acc_m=round(float(acc)) if acc is not None else MAX_ACCURACY_M,
        provider="opencellid",
    )
