# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.resource_service import ResourceService
from openviking.service.task_store import PersistentTaskStore
from openviking.service.task_tracker import TaskStatus, TaskTracker
from openviking.service.task_work_index import QueueTaskMetadata, TaskWorkIndex
from openviking_cli.session.user_id import UserIdentifier
from tests.test_task_tracker import _FakeAgfs


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "fail"])
async def test_skill_rollback_persists_cancellation_for_finished_work(monkeypatch, outcome):
    store = PersistentTaskStore(_FakeAgfs())
    tracker = TaskTracker(store=store)
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    ctx = RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)
    owner = {"account_id": ctx.account_id, "user_id": ctx.user.user_id}
    task = await tracker.create("add_skill", **owner)
    await getattr(tracker, outcome)(
        task.task_id, {} if outcome == "complete" else "failed", **owner
    )
    with pytest.raises(ValueError, match="already"):
        await tracker.cancel(task.task_id, **owner)
    await ResourceService.__new__(ResourceService).cancel_skill_processing(task.task_id, ctx)
    restarted = TaskTracker(store=store)
    restored = await restarted.get(task.task_id, **owner)
    assert restored.status == TaskStatus.CANCELLED
    assert restarted.is_cancellation_requested(task.task_id)
    if outcome == "fail":
        assert restored.error == "failed"


@pytest.mark.asyncio
async def test_skill_rollback_prevents_late_ack_failure_from_replaying_new_index(monkeypatch):
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    work = TaskWorkIndex()
    tracker.attach_work_index(work)
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    ctx = RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)
    owner = {"account_id": ctx.account_id, "user_id": ctx.user.user_id}
    task = await tracker.create("add_skill", **owner)
    child = QueueTaskMetadata(task.task_id, "last-vector", ctx.account_id, ctx.user.user_id)
    work.register("Embedding", child)
    await tracker.complete(task.task_id, {}, **owner)
    await work.prepare_ack("Embedding", child)
    assert (await tracker.get(task.task_id, **owner)).status == TaskStatus.COMPLETED
    await ResourceService.__new__(ResourceService).cancel_skill_processing(task.task_id, ctx)
    work.rollback_ack("Embedding", child)
    assert work.cancellation_requested(task.task_id)
    assert not work.register("Embedding", child)


@pytest.mark.asyncio
async def test_rollback_cancellation_does_not_apply_to_other_task_types():
    tracker = TaskTracker(store=PersistentTaskStore(_FakeAgfs()))
    owner = {"account_id": "account", "user_id": "user"}
    task = await tracker.create("add_resource", **owner)
    with pytest.raises(ValueError, match="Only Skill"):
        await tracker.cancel_skill_update_for_rollback(task.task_id, **owner)
    assert (await tracker.get(task.task_id, **owner)).status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_skill_rollback_does_not_ignore_failure_to_cancel_live_work(monkeypatch):
    tracker = SimpleNamespace(
        cancel_skill_update_for_rollback=AsyncMock(
            side_effect=ValueError("cancellation could not be persisted")
        ),
        has_work=lambda task_id: True,
    )
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    ctx = RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)
    with pytest.raises(ValueError, match="could not be persisted"):
        await ResourceService.__new__(ResourceService).cancel_skill_processing("update-task", ctx)


@pytest.mark.asyncio
async def test_skill_rollback_waits_for_work_to_exit_after_cancel_request(monkeypatch):
    cancelled = asyncio.Event()
    write_exited = asyncio.Event()

    async def cancel(*args, **kwargs):
        cancelled.set()
        return SimpleNamespace(status=TaskStatus.CANCELLING)

    tracker = SimpleNamespace(
        cancel_skill_update_for_rollback=cancel,
        has_work=lambda task_id: not write_exited.is_set(),
    )
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    ctx = RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)
    stopping = asyncio.create_task(
        ResourceService.__new__(ResourceService).cancel_skill_processing("update-task", ctx)
    )
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        assert not stopping.done()
        write_exited.set()
        await asyncio.wait_for(stopping, timeout=1)
    finally:
        write_exited.set()
        await asyncio.gather(stopping, return_exceptions=True)
