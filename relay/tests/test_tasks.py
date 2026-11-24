"""Tests for `app/tasks.py`: the TASKS_MODE switch and the CloudTasksQueue payload and target URL,
with the client library's `create_task` mocked."""

from __future__ import annotations

import json

import pytest

from app import tasks


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param((), tasks.InlineTaskQueue, id="default-inline"),
        pytest.param(("inline",), tasks.InlineTaskQueue, id="inline"),
        pytest.param(("cloud_tasks",), tasks.CloudTasksQueue, id="cloud-tasks"),
        pytest.param(("carrier-pigeon",), NotImplementedError, id="unknown-raises"),
    ],
)
def test_build_task_queue_maps_mode_to_queue_class(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...], expected: type
):
    monkeypatch.delenv("TASKS_MODE", raising=False)
    # The no-client branch builds a real tasks_v2.CloudTasksClient(), whose constructor calls
    # google.auth.default() and raises without GCP credentials; patch the class.
    from google.cloud import tasks_v2

    class _FakeClient:
        pass

    monkeypatch.setattr(tasks_v2, "CloudTasksClient", _FakeClient)

    if issubclass(expected, Exception):
        with pytest.raises(expected):
            tasks.build_task_queue(*args)
    else:
        assert isinstance(tasks.build_task_queue(*args), expected)


@pytest.mark.parametrize("service_account", ["relay-tasks@demo-pager.iam.gserviceaccount.com", None])
def test_build_task_targets_internal_task_with_oidc_token_only_when_service_account_set(
    monkeypatch: pytest.MonkeyPatch, service_account: str | None
):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://relay.example.com")
    if service_account:
        monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", service_account)
    else:
        monkeypatch.delenv("TASKS_SERVICE_ACCOUNT_EMAIL", raising=False)

    task = tasks.build_task("pager-retry:m_deadbeef:pgr-0001")

    http_request = task["http_request"]
    assert http_request["url"] == "https://relay.example.com/internal/task"
    assert http_request["http_method"] == "POST"
    assert json.loads(http_request["body"]) == {"name": "pager-retry:m_deadbeef:pgr-0001"}
    if service_account:
        assert http_request["oidc_token"] == {"service_account_email": service_account}
    else:
        assert "oidc_token" not in http_request


class _FakeCloudTasksClient:
    """Records every `create_task` call; never touches the network."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_task(self, *, parent: str, task: dict):
        self.calls.append({"parent": parent, "task": task})
        return task


def test_cloud_tasks_queue_enqueue_builds_correct_payload_and_target(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-pager")
    monkeypatch.setenv("TASKS_LOCATION", "us-central1")
    monkeypatch.setenv("TASKS_QUEUE", "relay-retries")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://relay.example.com")
    monkeypatch.delenv("TASKS_SERVICE_ACCOUNT_EMAIL", raising=False)

    fake_client = _FakeCloudTasksClient()
    queue = tasks.CloudTasksQueue(client=fake_client)

    called = False

    def _should_never_run() -> None:
        nonlocal called
        called = True

    queue.enqueue(_should_never_run, name="sms-retry:m_abc123:bid1")

    assert called is False
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["parent"] == "projects/demo-pager/locations/us-central1/queues/relay-retries"
    assert call["task"]["http_request"]["url"] == "https://relay.example.com/internal/task"
    assert json.loads(call["task"]["http_request"]["body"]) == {"name": "sms-retry:m_abc123:bid1"}


def test_cloud_tasks_queue_enqueue_swallows_create_task_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://relay.example.com")

    class _BrokenClient:
        def create_task(self, *, parent: str, task: dict):
            raise RuntimeError("cloud tasks unreachable")

    queue = tasks.CloudTasksQueue(client=_BrokenClient())
    # Must not raise: a retry-scheduling failure never propagates into the caller's request.
    queue.enqueue(lambda: None, name="whatever")
