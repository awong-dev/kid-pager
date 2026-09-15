"""The adapter contract every message backend implements.
Inbound replies are not part of it: an adapter registers its own webhook router."""

from __future__ import annotations

from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, DeliveryState, Message
from app.store.users import User


class DeliverResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ok: bool
    state: DeliveryState
    external_id: str | None = None
    error: str | None = None


class LinkStep(BaseModel):
    """What a user's client should do next to finish linking a backend."""

    model_config = ConfigDict(extra="ignore")

    instructions: str
    expiresInS: int | None = None


class NoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Backend(Protocol):
    kind: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]

    def deliver(self, msg: Message, delivery: Delivery, backend: BackendRow) -> DeliverResult: ...

    def start_link(self, user: User, backend: BackendRow) -> LinkStep | None: ...

    def complete_link(self, backend: BackendRow, proof: str) -> bool: ...

    def render_state(self, delivery: Delivery) -> str: ...
