# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Exercise Skill update failures through the HTTP API with local fake models."""

import asyncio
import threading
import zipfile

from openviking.storage.queuefs import QueueManager, get_queue_manager
from openviking.storage.queuefs.semantic_processor import SemanticProcessor
from openviking.utils.skill_processor import SkillProcessor
from tests.server.test_api_skills import _add_skill, _skill_md
from tests.server.test_api_skills import _stub_mcp_endpoint as _stub_mcp_endpoint


async def _upload_package(client, tmp_path, name, description, files):
    archive = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("SKILL.md", _skill_md(name, description))
        for path, content in files.items():
            package.writestr(path, content)
    with archive.open("rb") as handle:
        response = await client.post(
            "/api/v1/resources/temp_upload",
            files={"file": (archive.name, handle, "application/zip")},
        )
    assert response.status_code == 200, response.text
    return response.json()["result"]["temp_file_id"]


async def _wait_until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _download(client, uri):
    response = await client.get("/api/v1/content/download", params={"uri": uri})
    assert response.status_code == 200, response.text
    return response.content


async def _indexed_record(client, uri, level):
    response = await client.post(
        "/api/v1/search/find",
        json={
            "query": "",
            "target_uri": uri,
            "level": [level],
            "filter": {"op": "must", "field": "uri", "conds": [uri], "para": "-d=0"},
        },
    )
    assert response.status_code == 200, response.text
    return [
        (hit["uri"], hit["level"], hit["abstract"]) for hit in response.json()["result"]["skills"]
    ]


async def test_privacy_failure_restores_old_skill_before_background_enqueue(client, monkeypatch):
    name = "sync-privacy-rollback"
    queue = get_queue_manager().get_queue(QueueManager.SEMANTIC)
    original_enqueue = queue.enqueue
    enqueued = []

    async def record_enqueue(msg):
        enqueued.append(msg)
        return await original_enqueue(msg)

    monkeypatch.setattr(queue, "enqueue", record_enqueue)
    await _add_skill(client, name, "Original description")
    assert any(msg.context_type == "skill" and msg.uri.endswith(f"/{name}") for msg in enqueued)
    enqueued.clear()
    seeded = await client.post(
        f"/api/v1/privacy-configs/skill/{name}",
        json={"values": {"api_key": "old-value"}, "change_reason": "seed"},
    )
    assert seeded.status_code == 200, seeded.text
    before = (await client.get(f"/api/v1/skills/{name}")).json()["result"]

    original_apply = SkillProcessor.apply_skill_privacy

    async def apply_then_fail(self, skill_dict, privacy_values, ctx, **kwargs):
        await original_apply(self, skill_dict, {"api_key": "new-value"}, ctx, **kwargs)
        raise RuntimeError("injected privacy failure")

    monkeypatch.setattr(SkillProcessor, "apply_skill_privacy", apply_then_fail)
    response = await client.put(
        f"/api/v1/skills/{name}",
        json={"data": _skill_md(name, "Replacement description"), "wait": False},
    )
    assert response.status_code == 500, response.text
    assert enqueued == [], "A synchronous update failure must not leave background work"
    shown = await client.get(f"/api/v1/skills/{name}")
    assert shown.status_code == 200, shown.text
    for field in ("description", "abstract", "overview", "content"):
        assert shown.json()["result"][field] == before[field]
    privacy = await client.get(f"/api/v1/privacy-configs/skill/{name}")
    assert privacy.status_code == 200, privacy.text
    assert privacy.json()["result"]["current"]["values"] == {"api_key": "old-value"}


async def test_update_restores_all_deleted_privacy_versions_after_metadata_failure(
    client, monkeypatch
):
    name = "deleted-privacy-history-rollback"
    await _add_skill(client, name, "Original description")
    privacy_uri = f"/api/v1/privacy-configs/skill/{name}"
    for version in (1, 2):
        seeded = await client.post(
            privacy_uri,
            json={
                "values": {"api_key": f"test-only-value-{version}"},
                "change_reason": f"seed version {version}",
                "labels": {"revision": version},
            },
        )
        assert seeded.status_code == 200, seeded.text

    old_privacy = await client.get(privacy_uri)
    assert old_privacy.status_code == 200, old_privacy.text
    versions = await client.get(f"{privacy_uri}/versions")
    assert versions.status_code == 200, versions.text
    assert versions.json()["result"] == [1, 2]
    snapshots = {}
    for version in versions.json()["result"]:
        snapshot = await client.get(f"{privacy_uri}/versions/{version}")
        assert snapshot.status_code == 200, snapshot.text
        snapshots[version] = snapshot.json()["result"]

    async def prepare_without_privacy(self, skill_dict, ctx):
        return skill_dict, {}

    privacy_deleted_before_failure = False

    async def fail_metadata(*args, **kwargs):
        nonlocal privacy_deleted_before_failure
        privacy_deleted_before_failure = (await client.get(privacy_uri)).status_code == 404
        raise RuntimeError("injected source metadata failure after privacy deletion")

    monkeypatch.setattr(SkillProcessor, "prepare_skill_privacy", prepare_without_privacy)
    monkeypatch.setattr(
        "openviking.server.skill_source_metadata.write_skill_source_metadata", fail_metadata
    )
    response = await client.put(
        f"/api/v1/skills/{name}",
        json={
            "data": _skill_md(name, "Replacement description", "No private configuration."),
            "wait": True,
        },
    )
    assert response.status_code == 500, response.text
    assert privacy_deleted_before_failure, "The fault must happen after deleting old privacy"

    restored = await client.get(privacy_uri)
    assert restored.status_code == 200, restored.text
    assert restored.json()["result"] == old_privacy.json()["result"]
    restored_versions = await client.get(f"{privacy_uri}/versions")
    assert restored_versions.status_code == 200, restored_versions.text
    assert restored_versions.json()["result"] == versions.json()["result"]
    for version, snapshot in snapshots.items():
        restored_snapshot = await client.get(f"{privacy_uri}/versions/{version}")
        assert restored_snapshot.status_code == 200, restored_snapshot.text
        assert restored_snapshot.json()["result"] == snapshot
    shown = await client.get(f"/api/v1/skills/{name}")
    assert shown.status_code == 200, shown.text
    assert shown.json()["result"]["description"] == "Original description"


async def test_update_timeout_cancels_unfinished_work_and_restores_package_and_index(
    client, tmp_path, monkeypatch
):
    from openviking.storage.queuefs import semantic_dag

    name = "timeout-package-rollback"
    old_upload = await _upload_package(
        client,
        tmp_path,
        name,
        "Original description",
        {"references/old.md": "Original recovery instructions."},
    )
    added = await client.post("/api/v1/skills", json={"temp_file_id": old_upload, "wait": True})
    assert added.status_code == 200, added.text
    root = added.json()["result"]["root_uri"]
    paths = (
        "SKILL.md",
        ".abstract.md",
        ".overview.md",
        "references/old.md",
        "references/.abstract.md",
        "references/.overview.md",
    )
    old_contents = {path: await _download(client, f"{root}/{path}") for path in paths}
    record_locations = (
        (root, 0),
        (root, 1),
        (f"{root}/SKILL.md", 2),
        (f"{root}/references", 0),
        (f"{root}/references", 1),
        (f"{root}/references/old.md", 2),
    )
    old_records = {
        location: await _indexed_record(client, *location) for location in record_locations
    }
    assert all(len(records) == 1 for records in old_records.values())

    files = {"references/000-fast.md": "A replacement file that reaches the index."}
    files.update({f"references/slow-{index}.md": "Unfinished replacement." for index in range(8)})
    new_upload = await _upload_package(client, tmp_path, name, "Replacement description", files)
    release = threading.Event()
    fast_finished = threading.Event()
    started = set()
    cancelled = set()
    finished = set()
    original_summary = SemanticProcessor._generate_single_file_summary
    original_scheduler = semantic_dag.get_semantic_node_scheduler

    async def hold_summary(self, file_path, *args, **kwargs):
        if file_path.endswith("/000-fast.md"):
            result = await original_summary(self, file_path, *args, **kwargs)
            fast_finished.set()
            return result
        started.add(file_path)
        try:
            while not release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.add(file_path)
            raise
        finished.add(file_path)
        return await original_summary(self, file_path, *args, **kwargs)

    monkeypatch.setattr(SemanticProcessor, "_generate_single_file_summary", hold_summary)
    monkeypatch.setattr(
        semantic_dag, "get_semantic_node_scheduler", lambda _workers: original_scheduler(2)
    )
    updating = asyncio.create_task(
        client.put(
            f"/api/v1/skills/{name}",
            json={"temp_file_id": new_upload, "wait": True, "timeout": 2},
        )
    )
    try:
        await _wait_until(lambda: fast_finished.is_set() and len(started) == 2)
        async with asyncio.timeout(1):
            while not await _indexed_record(client, f"{root}/references/000-fast.md", 2):
                await asyncio.sleep(0.01)
        response = await asyncio.wait_for(asyncio.shield(updating), timeout=5)
        assert response.status_code == 504, response.text
        assert response.json()["error"]["code"] == "DEADLINE_EXCEEDED"
        assert cancelled == started
        assert not finished, "Rollback must not wait for blocked summaries to finish normally"
        assert len(started) < len(files), "Pending summaries should be skipped on cancellation"

        # Releasing the fault after the response must not let stale work modify the restored pack.
        release.set()
        await get_queue_manager().wait_complete(timeout=5)
        for path, content in old_contents.items():
            assert await _download(client, f"{root}/{path}") == content
        for location, records in old_records.items():
            assert await _indexed_record(client, *location) == records
        for path in files:
            missing = await client.get("/api/v1/content/download", params={"uri": f"{root}/{path}"})
            assert missing.status_code == 404, missing.text
            assert await _indexed_record(client, f"{root}/{path}", 2) == []
    finally:
        release.set()
        await get_queue_manager().wait_complete(timeout=5)
        if not updating.done():
            updating.cancel()
        await asyncio.gather(updating, return_exceptions=True)


async def test_async_update_background_failure_keeps_successfully_returned_replacement(
    client, monkeypatch
):
    name = "async-failure-keeps-replacement"
    await _add_skill(client, name, "Original description")
    release_failure = threading.Event()

    async def fail_after_response(self, file_path, *args, **kwargs):
        while not release_failure.is_set():
            await asyncio.sleep(0.01)
        raise RuntimeError("injected background summary failure")

    monkeypatch.setattr(SemanticProcessor, "_generate_single_file_summary", fail_after_response)
    try:
        response = await client.put(
            f"/api/v1/skills/{name}",
            json={"data": _skill_md(name, "Replacement description"), "wait": False},
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["result"]["task_id"]
        release_failure.set()
        async with asyncio.timeout(5):
            while True:
                task_response = await client.get(f"/api/v1/tasks/{task_id}")
                assert task_response.status_code == 200, task_response.text
                status = task_response.json()["result"]["status"]
                if status in {"failed", "cancelled", "completed"}:
                    break
                await asyncio.sleep(0.01)
        assert status == "failed", task_response.text
        shown = await client.get(f"/api/v1/skills/{name}")
        assert shown.status_code == 200, shown.text
        assert shown.json()["result"]["description"] == "Replacement description"
    finally:
        release_failure.set()
        await get_queue_manager().wait_complete(timeout=5)


async def test_update_reports_backup_restoration_failure(client, service, monkeypatch):
    name = "failed-rollback-is-visible"
    added = await _add_skill(client, name, "Original description")
    root = added["root_uri"]
    original_copy = service.viking_fs._copy_directory_under_tree_locks

    async def fail_privacy(*args, **kwargs):
        raise RuntimeError("injected privacy failure")

    async def fail_backup_restore(*args, **kwargs):
        if ".update-backup-" in kwargs["old_uri"] and kwargs["new_uri"] == root:
            raise RuntimeError("injected backup restoration failure")
        return await original_copy(*args, **kwargs)

    monkeypatch.setattr(SkillProcessor, "apply_skill_privacy", fail_privacy)
    monkeypatch.setattr(service.viking_fs, "_copy_directory_under_tree_locks", fail_backup_restore)
    response = await client.put(
        f"/api/v1/skills/{name}",
        json={"data": _skill_md(name, "Replacement description"), "wait": False},
    )
    assert response.status_code == 500, response.text
    assert "injected backup restoration failure" in response.text
