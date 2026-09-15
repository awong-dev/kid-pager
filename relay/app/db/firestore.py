"""firebase-admin / Cloud Firestore init, emulator-aware, and the transaction helper."""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import firebase_admin
from firebase_admin import credentials
from google.api_core.exceptions import Aborted
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore import Client, DocumentReference, Transaction, transactional

T = TypeVar("T")

# `run_transaction` retries on a fresh transaction with full-jitter exponential backoff;
# worst-case sleep ~6.3 s, under the broker webhook's 10 s `request_ttl`.
EXTRA_RETRY_ATTEMPTS = 6
EXTRA_RETRY_BASE_DELAY_S = 0.1
EXTRA_RETRY_MAX_DELAY_S = 3.2

_lock = threading.Lock()
_app: firebase_admin.App | None = None


class EmulatorCredentials(credentials.Base):
    """Anonymous credentials for emulators, where ADC lookup would raise."""

    def get_credential(self) -> AnonymousCredentials:
        return AnonymousCredentials()


def _using_emulators() -> bool:
    return bool(
        os.environ.get("FIRESTORE_EMULATOR_HOST") or os.environ.get("FIREBASE_AUTH_EMULATOR_HOST")
    )


def get_app() -> firebase_admin.App:
    """The process-wide `firebase_admin.App`, created on first use; thread-safe."""
    global _app
    with _lock:
        if _app is not None:
            return _app
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
        options: dict[str, str] = {"projectId": project_id} if project_id else {}
        if _using_emulators():
            cred: credentials.Base = EmulatorCredentials()
        else:
            cred = credentials.ApplicationDefault()
        _app = firebase_admin.initialize_app(cred, options)
        return _app


    global _db
    with _lock:
        if _db is not None:
            return _db
    with _lock:
        if _db is None:
        return _db


def reset_for_tests() -> None:
    """Tears down the cached app/client so the next use creates a fresh one. Tests only."""
    global _app, _db
    with _lock:
        if _app is not None:
            try:
                firebase_admin.delete_app(_app)
            except ValueError:
                pass
        _app = None
        _db = None


def run_transaction[T](fn: Callable[[Transaction], T]) -> T:
    """Runs `fn(transaction)` in a Firestore transaction, reading via `transaction.get` only."""
    db = get_db()
    last_exc: BaseException | None = None
    for attempt in range(EXTRA_RETRY_ATTEMPTS + 1):
        transaction = db.transaction()

        @transactional
        def _run(txn: Transaction) -> T:
            return fn(txn)

        try:
            return _run(transaction)
        except Aborted as exc:
            last_exc = exc
        except ValueError as exc:
            if not isinstance(exc.__cause__, Aborted):
                raise
            last_exc = exc
        if attempt < EXTRA_RETRY_ATTEMPTS:
            cap = min(EXTRA_RETRY_MAX_DELAY_S, EXTRA_RETRY_BASE_DELAY_S * (2**attempt))
            time.sleep(random.uniform(0, cap))
    assert last_exc is not None
    raise last_exc


def doc_exists_in_txn(transaction: Transaction, ref: DocumentReference) -> bool:
    """`True` if `ref` has a document, read inside `transaction`."""
    snap = ref.get(transaction=transaction)
    return snap.exists
