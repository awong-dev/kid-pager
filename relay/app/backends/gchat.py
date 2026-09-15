"""`gchat` backend: Google Chat app.
Outbound uses the relay's ADC credentials; linking is inbound-driven (`/link <code>`)."""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Mapping
from typing import Any, Protocol

import google.auth as google_auth
import google.auth.jwt as google_jwt
import google.auth.transport.requests as google_requests
import google.oauth2.id_token as google_id_token
from pydantic import BaseModel, ConfigDict

from app.backends.base import DeliverResult, LinkStep
from app.store import backends as backends_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message
from app.store.users import User

logger = logging.getLogger("relay.backends.gchat")

CHAT_API_BASE_URL = "https://chat.googleapis.com/v1"
# Google Chat's cert endpoint for verifying its own bearer tokens.
DEFAULT_CERTS_URL = (
    "https://www.googleapis.com/service_accounts/v1/metadata/x509/"
    "chat@system.gserviceaccount.com"
)
CHAT_ISSUER = "chat@system.gserviceaccount.com"
LINK_CODE_TTL_S = 600
LOC_PREVIEW = "location"


def gchat_audience() -> str:
    """The Chat app's project-number audience; blank never verifies (fail closed)."""
    return os.environ.get("GCHAT_AUDIENCE", "")


def gchat_certs_url() -> str:
    return os.environ.get("GCHAT_CERTS_URL", DEFAULT_CERTS_URL)


def verify_chat_bearer_token(
    token: str,
    *,
    audience: str,
    certs: Mapping[str, str] | None = None,
    certs_url: str | None = None,
) -> dict[str, Any]:
    """Raises `ValueError` on a bad signature/audience/issuer/expiry; `certs` skips the network."""
    if not audience:
        raise ValueError("no GCHAT_AUDIENCE configured for this deployment")
    # 30 s clock-skew tolerance: the library default of 0 gives spurious 401s from
    # sub-second drift between Google's signer and this instance.
    if certs is not None:
        claims = google_jwt.decode(
            token, certs=dict(certs), audience=audience, clock_skew_in_seconds=30
        )
    else:
        request = google_requests.Request()
        claims = google_id_token.verify_token(
            token,
            request,
            audience=audience,
            certs_url=certs_url or DEFAULT_CERTS_URL,
            clock_skew_in_seconds=30,
        )
    issuer = claims.get("iss")
    if issuer != CHAT_ISSUER:
        raise ValueError(f"unexpected issuer: {issuer!r}")
    return claims


class GChatConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    space: str | None = None


class ChatClient(Protocol):
    """Stubbable stand-in for the real `spaces.messages.create` call."""

    def send_message(self, space: str, text: str) -> str | None: ...


class NullChatClient:
    """Default in dev/test; never touches the real Chat API."""

    def send_message(self, space: str, text: str) -> str | None:
        return None


class GoogleChatClient:
    """Real client: ADC (`google.auth.default()`), no shared secret."""

    def __init__(self) -> None:
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google_auth.default(
            scopes=["https://www.googleapis.com/auth/chat.bot"]
        )
        self._session = AuthorizedSession(credentials)

    def send_message(self, space: str, text: str) -> str | None:
        resp = self._session.post(
            f"{CHAT_API_BASE_URL}/{space}/messages", json={"text": text}, timeout=5.0
        )
        if 200 <= resp.status_code < 300:
            return resp.json().get("name")
        logger.warning("gchat send rejected (space=%s status=%s)", space, resp.status_code)
        return None


def _render_body(msg: Message) -> str:
    if msg.body:
        return msg.body
    if msg.kind == "loc" and msg.loc is not None:
        return LOC_PREVIEW
    return "(no content)"


class GChatBackend:
    kind = "gchat"
    config_schema = GChatConfig

    def __init__(self, chat_client: ChatClient | None = None) -> None:
        self._chat = chat_client or NullChatClient()

    def deliver(self, msg: Message, delivery: Delivery, backend: BackendRow) -> DeliverResult:
        space = backend.config.get("space")
        if not space:
            # Not linked yet; recoverable (a later tick retries once `/link` completes).
            return DeliverResult(ok=False, state="queued", error="gchat backend not linked yet")
        text = _render_body(msg)
        try:
            message_name = self._chat.send_message(space, text)
        except Exception as exc:  # noqa: BLE001 -- best-effort delivery
            logger.warning("gchat send failed (space=%s): %r", space, exc)
            return DeliverResult(ok=False, state="queued", error=str(exc))
        return DeliverResult(ok=True, state="sent", external_id=message_name)

    def start_link(self, user: User, backend: BackendRow) -> LinkStep | None:
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = int(time.time()) + LINK_CODE_TTL_S
        backends_store.set_gchat_link_code(code, user.uid, backend.id, expires_at)
        # Also stored in the backend's `config`, which the web page's Firestore listener
        # already reads.
        backends_store.update_backend(
            user.uid,
            backend.id,
            config={**backend.config, "linkCode": code, "linkCodeExpiresAt": expires_at},
        )
        return LinkStep(
            instructions=(
                f"Open a DM with the Pager bot in Google Chat and send: /link {code}"
            ),
            expiresInS=LINK_CODE_TTL_S,
        )

    def complete_link(self, backend: BackendRow, proof: str) -> bool:
        # Real completion happens in the inbound webhook, which has the (uid, bid) pair
        # from `pop_gchat_link_code`.
        return False

    def render_state(self, delivery: Delivery) -> str:
        return delivery.state
