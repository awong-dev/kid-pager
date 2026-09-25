"""Real FCM push backend (`FirebaseFCMClient`, firebase_admin multicast send).
A token FCM reports dead is deleted; any other per-token failure is logged and kept."""

from __future__ import annotations

import logging

from firebase_admin import messaging

from app.store import push_tokens as push_tokens_store

logger = logging.getLogger("relay.backends.fcm")

# `send_each_for_multicast` caps at 500 tokens per call (FCM's own limit).
_CHUNK_SIZE = 500

# High priority so a phone wakes for it; a multi-hour TTL so a phone that was off
# still gets the latest message on reconnect.
_WEBPUSH_HEADERS = {"Urgency": "high", "TTL": "14400"}

_DEAD_TOKEN_ERRORS = (messaging.UnregisteredError, messaging.SenderIdMismatchError)


class FirebaseFCMClient:
    """`backends.webapp.FCMClient` implementation backed by real FCM."""

    def send_data(self, tokens: list[str], data: dict[str, str]) -> None:
        for start in range(0, len(tokens), _CHUNK_SIZE):
            self._send_chunk(tokens[start : start + _CHUNK_SIZE], data)

    def _send_chunk(self, tokens: list[str], data: dict[str, str]) -> None:
        message = messaging.MulticastMessage(
            tokens=tokens,
            data=data,
            webpush=messaging.WebpushConfig(headers=dict(_WEBPUSH_HEADERS)),
        )
        response = messaging.send_each_for_multicast(message)
        for token, result in zip(tokens, response.responses, strict=True):
            if result.success:
                continue
            exc = result.exception
            if isinstance(exc, _DEAD_TOKEN_ERRORS):
                push_tokens_store.remove_token_anywhere(token)
            else:
                # Log a prefix only: a push token is a capability to notify
                # that browser, so keep the full value out of the logs.
                logger.info("FCM send failed for token %s…: %s", token[:12], exc)
