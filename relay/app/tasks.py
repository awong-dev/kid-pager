"""Delivery-retry task queue, selected by `TASKS_MODE`: `inline` runs `fn()` in the caller.
`cloud_tasks` enqueues an HTTP task with only `name`; there is no `/internal/task` handler."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger("relay.tasks")


class TaskQueue(Protocol):
    def enqueue(self, fn: Callable[[], None], *, name: str) -> None: ...


class InlineTaskQueue:
    """`TASKS_MODE=inline`: runs `fn` synchronously and logs any exception."""

    def enqueue(self, fn: Callable[[], None], *, name: str) -> None:
        try:
            fn()
        except Exception:
            logger.exception("inline task %r raised", name)


def _project() -> str:
    return os.environ.get("GOOGLE_CLOUD_PROJECT", "")


def _location() -> str:
    return os.environ.get("TASKS_LOCATION", "us-central1")


def _queue() -> str:
    return os.environ.get("TASKS_QUEUE", "relay-retries")


def _target_url() -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/internal/task"


def _service_account_email() -> str:
    return os.environ.get("TASKS_SERVICE_ACCOUNT_EMAIL", "")


def queue_path(*, project: str | None = None, location: str | None = None, queue: str | None = None) -> str:
    """`projects/{project}/locations/{location}/queues/{queue}`, the `parent` for `create_task`."""
    project = project if project is not None else _project()
    location = location if location is not None else _location()
    queue = queue if queue is not None else _queue()
    return f"projects/{project}/locations/{location}/queues/{queue}"


def build_task(name: str) -> dict:
    """The Task dict `enqueue()` sends; Cloud Tasks attaches the OIDC token at dispatch."""
    import json

    body = json.dumps({"name": name}).encode("utf-8")
    task: dict = {
        "http_request": {
            "http_method": "POST",
            "url": _target_url(),
            "headers": {"Content-Type": "application/json"},
            "body": body,
        }
    }
    sa_email = _service_account_email()
    if sa_email:
        task["http_request"]["oidc_token"] = {"service_account_email": sa_email}
    return task


class CloudTasksQueue:
    """`TASKS_MODE=cloud_tasks`: one HTTP task per `enqueue()`; failures are logged."""

    def __init__(self, client: object | None = None) -> None:
        if client is None:
            from google.cloud import tasks_v2

            client = tasks_v2.CloudTasksClient()
        self._client = client

    def enqueue(self, fn: Callable[[], None], *, name: str) -> None:
        # `fn` is unused: only `name` is enqueued. Kept so this stays a drop-in `TaskQueue`.
        del fn
        try:
            self._client.create_task(parent=queue_path(), task=build_task(name))
        except Exception:
            logger.exception("cloud tasks enqueue %r failed", name)


def build_task_queue(mode: str | None = None) -> TaskQueue:
    """`mode` defaults to the `TASKS_MODE` env var (default `inline`)."""
    mode = mode or os.environ.get("TASKS_MODE", "inline")
    if mode == "inline":
        return InlineTaskQueue()
    if mode == "cloud_tasks":
        return CloudTasksQueue()
    raise NotImplementedError(
        f"TASKS_MODE={mode!r} is not implemented -- only 'inline' and 'cloud_tasks' exist "
        "(docs/SERVER_PLAN.md §5's `tasks.py` line)."
    )
