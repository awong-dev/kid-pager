"""`/api/admin/hooks` (super only); the secret appears only in create and `rotate-secret`."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import AuthedUser, require_super
from app.routers.admin import require_admin_write_rate_limit
from app.store import hooks as hooks_store
from app.store.hooks import FailMode, Hook, HookMode, HookStats

router = APIRouter(prefix="/api/admin/hooks", dependencies=[Depends(require_super)])

TIMEOUT_MIN_MS = 100
TIMEOUT_MAX_MS = 10000


def _check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be http(s)://host[:port]/path")
    return url


def _check_families(value: list[str] | Literal["*"]) -> list[str] | Literal["*"]:
    if value == "*":
        return value
    if not value or any(not isinstance(f, str) or not f for f in value):
        raise ValueError('families must be "*" or a non-empty list of family ids')
    return value


class HookOut(BaseModel):
    """A hook without its secret."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    url: str
    mode: HookMode
    families: list[str] | Literal["*"]
    order: int
    timeoutMs: int
    failMode: FailMode
    context: int
    enabled: bool
    createdBy: str | None = None
    stats: HookStats = Field(default_factory=HookStats)


class HookWithSecret(HookOut):
    secret: str


def _out(hook: Hook) -> HookOut:
    return HookOut.model_validate(hook.model_dump())


class CreateHookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    url: str
    mode: HookMode
    families: list[str] | Literal["*"] = "*"
    order: int = 0
    timeoutMs: int = Field(default=2000, ge=TIMEOUT_MIN_MS, le=TIMEOUT_MAX_MS)
    failMode: FailMode = "open"
    context: int = Field(default=20, ge=0, le=50)
    enabled: bool = True

    _url = field_validator("url")(_check_url)
    _families = field_validator("families")(_check_families)


class PatchHookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    url: str | None = None
    mode: HookMode | None = None
    families: list[str] | Literal["*"] | None = None
    order: int | None = None
    timeoutMs: int | None = Field(default=None, ge=TIMEOUT_MIN_MS, le=TIMEOUT_MAX_MS)
    failMode: FailMode | None = None
    context: int | None = Field(default=None, ge=0, le=50)
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else _check_url(v)

    @field_validator("families")
    @classmethod
    def _families(
        cls, v: list[str] | Literal["*"] | None
    ) -> list[str] | Literal["*"] | None:
        return None if v is None else _check_families(v)


@router.get("")
def list_hooks() -> list[HookOut]:
    return [_out(h) for h in hooks_store.list_hooks()]


@router.post(
    "", status_code=201, dependencies=[Depends(require_admin_write_rate_limit)]
)
def create_hook(
    req: CreateHookRequest, authed: Annotated[AuthedUser, Depends(require_super)]
) -> HookWithSecret:
    try:
        hook = hooks_store.create(
            name=req.name,
            url=req.url,
            mode=req.mode,
            families=req.families,
            order=req.order,
            timeout_ms=req.timeoutMs,
            fail_mode=req.failMode,
            context=req.context,
            enabled=req.enabled,
            created_by=authed.uid,
        )
    except hooks_store.HookLimit:
        raise HTTPException(
            status_code=409, detail=f"at most {hooks_store.MAX_HOOKS} hooks"
        ) from None
    return HookWithSecret.model_validate(hook.model_dump())


@router.get("/{hook_id}")
def get_hook(hook_id: str) -> HookOut:
    hook = hooks_store.get(hook_id)
    if hook is None:
        raise HTTPException(status_code=404, detail="no such hook")
    return _out(hook)


@router.patch("/{hook_id}", dependencies=[Depends(require_admin_write_rate_limit)])
def patch_hook(hook_id: str, req: PatchHookRequest) -> HookOut:
    fields = req.model_dump(exclude_none=True)
    hook = hooks_store.update(hook_id, fields)
    if hook is None:
        raise HTTPException(status_code=404, detail="no such hook")
    return _out(hook)


@router.post(
    "/{hook_id}/rotate-secret", dependencies=[Depends(require_admin_write_rate_limit)]
)
def rotate_hook_secret(hook_id: str) -> HookWithSecret:
    hook = hooks_store.rotate_secret(hook_id)
    if hook is None:
        raise HTTPException(status_code=404, detail="no such hook")
    return HookWithSecret.model_validate(hook.model_dump())


@router.delete(
    "/{hook_id}",
    status_code=204,
    dependencies=[Depends(require_admin_write_rate_limit)],
)
def delete_hook(hook_id: str) -> None:
    if not hooks_store.delete(hook_id):
        raise HTTPException(status_code=404, detail="no such hook")
