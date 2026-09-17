# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.storage import resource_diff


def test_context_plan_has_explicit_actions_and_compact_semantic_roundtrip():
    from openviking.storage.context_update_plan import (
        ContextUpdatePlan,
        IndexSlot,
        SemanticAction,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )

    entry = SemanticTreeEntry(
        relative_path="a.py",
        kind="file",
        content_state="modified",
        semantic_action=SemanticAction.GENERATE,
        md5="new",
        index_slots=(
            IndexSlot(
                level=2,
                record_id="external-id",
                existing_fields={"abstract": "old"},
                operation="upsert",
                trigger="output_changed",
                fields={"search_tags": ["new"]},
            ),
        ),
    )
    plan = ContextUpdatePlan(
        root_uri="viking://resources/repo",
        context_type="resource",
        semantic_plan=SemanticPlan(
            root_uri="viking://resources/repo",
            context_type="resource",
            tree=SemanticTreeSnapshot(
                (
                    SemanticTreeEntry("", "directory", "unchanged", "aggregate"),
                    entry,
                )
            ),
        ),
    )
    payload = plan.to_dict()
    restored = ContextUpdatePlan.from_dict(payload)
    assert restored == plan
    node = next(
        item
        for item in payload["semantic_plan"]["tree"]["entries"]
        if item["relative_path"] == "a.py"
    )
    assert "indexed_records" not in node
    assert "previous_abstracts" not in node
    assert "uri" not in node["index_slots"][0]
    assert node["index_slots"][0]["record_id"] == "external-id"


def test_after_content_commit_keeps_only_derived_actions():
    from openviking.storage.context_update_plan import (
        ContentTreeAction,
        ContextUpdatePlan,
        IndexAction,
    )

    plan = ContextUpdatePlan(
        "viking://resources/repo",
        "resource",
        content_tree_actions=(
            ContentTreeAction(
                "upsert",
                "a.py",
                new_kind="file",
                artifact_path="repository/a.py",
                md5="new",
            ),
        ),
        direct_index_actions=(
            IndexAction("delete", "viking://resources/repo/old.py", 2, "old-l2"),
        ),
    )

    committed = plan.after_content_commit()

    assert committed.content_tree_actions == ()
    assert committed.semantic_plan is plan.semantic_plan
    assert committed.direct_index_actions is plan.direct_index_actions


def test_context_plan_rejects_conflicting_record_operations():
    from openviking.storage.context_update_plan import (
        ContextUpdatePlan,
        IndexAction,
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )

    semantic = SemanticPlan(
        root_uri="viking://resources/repo",
        context_type="resource",
        tree=SemanticTreeSnapshot(
            (
                SemanticTreeEntry("", "directory", "unchanged", "aggregate"),
                SemanticTreeEntry(
                    "a.py",
                    "file",
                    "modified",
                    "generate",
                    md5="new",
                    index_slots=(
                        IndexSlot(
                            2,
                            "same-id",
                            {"abstract": "old"},
                            operation="upsert",
                            trigger="output_ready",
                        ),
                    ),
                ),
            )
        ),
    )
    with pytest.raises(ValueError, match="conflict"):
        ContextUpdatePlan(
            root_uri=semantic.root_uri,
            context_type="resource",
            semantic_plan=semantic,
            direct_index_actions=(
                IndexAction("delete", semantic.root_uri + "/a.py", 2, "same-id"),
            ),
        )


def test_semantic_plan_rejects_disconnected_or_mistyped_actions():
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )

    with pytest.raises(ValueError, match="root"):
        SemanticPlan(
            "viking://resources/repo",
            "resource",
            SemanticTreeSnapshot(
                (
                    SemanticTreeEntry(
                        "src/a.py",
                        "file",
                        "modified",
                        "generate",
                        index_slots=(
                            IndexSlot(2, "a-l2", operation="upsert", trigger="output_ready"),
                        ),
                    ),
                )
            ),
        )
    with pytest.raises(ValueError, match="directory.*aggregate"):
        SemanticTreeEntry("src", "directory", "modified", "generate")
    with pytest.raises(ValueError, match="file.*generate"):
        SemanticTreeEntry("a.py", "file", "modified", "aggregate", md5="m")
    with pytest.raises(ValueError, match="ancestor.*aggregate"):
        SemanticPlan(
            "viking://resources/repo",
            "resource",
            SemanticTreeSnapshot(
                (
                    SemanticTreeEntry(
                        "",
                        "directory",
                        "unchanged",
                        "reuse",
                        index_slots=(IndexSlot(0, "root-l0", {"abstract": "root"}),),
                    ),
                    SemanticTreeEntry(
                        "a.py",
                        "file",
                        "modified",
                        "generate",
                        md5="new",
                        index_slots=(
                            IndexSlot(2, "a-l2", operation="upsert", trigger="output_ready"),
                        ),
                    ),
                )
            ),
        )


def test_semantic_plan_validates_ancestors_without_rescanning_entries():
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )

    class CountingEntries:
        def __init__(self, entries):
            self._entries = tuple(entries)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            return iter(self._entries)

    directories = [
        "/".join(["src", *[f"level-{index}" for index in range(depth)]]) for depth in range(8)
    ]
    entries = CountingEntries(
        [SemanticTreeEntry("", "directory", "unchanged", "aggregate")]
        + [SemanticTreeEntry(path, "directory", "unchanged", "aggregate") for path in directories]
        + [
            SemanticTreeEntry(
                directories[-1] + "/a.py",
                "file",
                "modified",
                "generate",
                md5="new",
                index_slots=(IndexSlot(2, "a-l2", operation="upsert", trigger="output_ready"),),
            )
        ]
    )

    SemanticPlan(
        "viking://resources/repo",
        "resource",
        SemanticTreeSnapshot(entries),
    )

    assert entries.iterations <= 4


def test_semantic_plan_rejects_missing_higher_ancestor_independent_of_entry_order():
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )

    with pytest.raises(ValueError, match="lacks ancestor 'a'.*a/b/c.py"):
        SemanticPlan(
            "viking://resources/repo",
            "resource",
            SemanticTreeSnapshot(
                (
                    SemanticTreeEntry("", "directory", "unchanged", "aggregate"),
                    SemanticTreeEntry(
                        "a/b/c.py",
                        "file",
                        "modified",
                        "generate",
                        md5="new",
                        index_slots=(
                            IndexSlot(2, "c-l2", operation="upsert", trigger="output_ready"),
                        ),
                    ),
                    SemanticTreeEntry("a/b", "directory", "unchanged", "aggregate"),
                )
            ),
        )


def test_index_slot_rejects_trigger_without_operation():
    from openviking.storage.context_update_plan import IndexSlot

    with pytest.raises(ValueError, match="trigger"):
        IndexSlot(2, "a-l2", operation="none", trigger="output_ready")
    with pytest.raises(ValueError, match="only supports"):
        IndexSlot(2, "a-l2", operation="update_fields", trigger="output_ready")


def test_reuse_entry_rejects_index_mutation():
    from openviking.storage.context_update_plan import IndexSlot, SemanticTreeEntry

    with pytest.raises(ValueError, match="reuse.*index"):
        SemanticTreeEntry(
            "a.py",
            "file",
            "unchanged",
            "reuse",
            index_slots=(
                IndexSlot(
                    2,
                    "a-l2",
                    {"abstract": "old"},
                    operation="upsert",
                    trigger="output_ready",
                ),
            ),
        )


@pytest.mark.asyncio
async def test_resolver_compares_missing_fingerprints_with_bounded_reads():
    from openviking.storage.resource_rnfv import (
        FormalTreeSnapshot,
        NewArtifactSnapshot,
        RequestIntent,
        RNFVSnapshot,
        VectorIndexSnapshot,
    )
    from openviking.storage.viking_fs._diff_plan import NewEntry, TargetFile

    paths = [f"{i}.py" for i in range(8)]
    snapshot = RNFVSnapshot(
        RequestIntent("viking://resources/repo", "semantic_and_vectors"),
        NewArtifactSnapshot({p: NewEntry() for p in paths}),
        FormalTreeSnapshot({p: TargetFile() for p in paths}),
        VectorIndexSnapshot({}, frozenset({"id", "uri", "level", "md5"})),
    )
    active = peak = 0
    gate = asyncio.Event()

    async def read(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            gate.set()
        try:
            await asyncio.wait_for(gate.wait(), 1)
            return b"same"
        finally:
            active -= 1

    resolve = getattr(resource_diff, "resolve_resource_diff", None)
    assert resolve is not None
    result = await resolve(
        snapshot,
        store=SimpleNamespace(read_bytes=read),
        artifact_ref=None,
        target=SimpleNamespace(read_file=read),
        concurrency=2,
    )
    assert peak == 4
    assert active == 0
    assert all(
        e.content_state == "unchanged" and e.index_state == "missing"
        for e in result.entries.values()
    )
    assert all(e.md5 for e in result.entries.values())
    assert not hasattr(result, "needs_body_compare")


@pytest.mark.asyncio
async def test_resolver_rejects_incomplete_snapshot_before_io():
    from openviking.storage.resource_rnfv import (
        FormalTreeSnapshot,
        NewArtifactSnapshot,
        RequestIntent,
        RNFVSnapshot,
        VectorIndexSnapshot,
    )

    snapshot = RNFVSnapshot(
        RequestIntent("viking://resources/repo", "semantic_and_vectors"),
        NewArtifactSnapshot({}, complete=False),
        FormalTreeSnapshot({}),
        VectorIndexSnapshot({}, frozenset({"id", "uri", "level", "md5"})),
    )
    store, target = AsyncMock(), AsyncMock()
    resolve = getattr(resource_diff, "resolve_resource_diff", None)
    assert resolve is not None
    with pytest.raises(ValueError, match="incomplete"):
        await resolve(snapshot, store=store, artifact_ref=None, target=target)
    store.read_bytes.assert_not_called()
    target.read_file.assert_not_called()


@pytest.mark.asyncio
async def test_resolver_hashes_new_file_when_manifest_md5_is_missing():
    from openviking.storage.context_update_plan import ContentState
    from openviking.storage.resource_rnfv import (
        FormalTreeSnapshot,
        NewArtifactSnapshot,
        RequestIntent,
        RNFVSnapshot,
        VectorIndexSnapshot,
    )
    from openviking.storage.viking_fs._diff_plan import NewEntry
    from openviking.utils.content_hash import content_md5

    snapshot = RNFVSnapshot(
        RequestIntent("viking://resources/repo", "semantic_and_vectors"),
        NewArtifactSnapshot({"a.py": NewEntry()}),
        FormalTreeSnapshot({}),
        VectorIndexSnapshot({}, frozenset({"id", "uri", "level", "md5"})),
    )
    store = AsyncMock()
    store.read_bytes.return_value = b"new body"
    result = await resource_diff.resolve_resource_diff(
        snapshot,
        store=store,
        artifact_ref=object(),
        target=AsyncMock(),
        artifact_paths={"a.py": "repository/a.py"},
    )

    assert result.entries["a.py"].content_state is ContentState.ADDED
    assert result.entries["a.py"].md5 == content_md5(b"new body")
    store.read_bytes.assert_awaited_once()


def test_builder_maps_content_semantic_and_direct_index_actions():
    from openviking.storage.context_update_plan import (
        ContentState,
        ContextUpdatePlan,
        IndexOperation,
        IndexState,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    diff = ResourceDiffResult(
        entries={
            "changed.py": ResourceDiffEntry(
                "changed.py",
                ContentState.MODIFIED,
                IndexState.STALE,
                old_kind="file",
                new_kind="file",
                md5="new-md5",
            ),
            "gone.py": ResourceDiffEntry(
                "gone.py", ContentState.DELETED, IndexState.ORPHAN, old_kind="file"
            ),
            "ghost.py": ResourceDiffEntry("ghost.py", ContentState.ABSENT, IndexState.ORPHAN),
        }
    )
    inventory = {
        "changed-id": VectorRecordSnapshot(
            "changed-id",
            "viking://resources/repo/changed.py",
            "changed.py",
            2,
            {"md5": "old-md5", "abstract": "old abstract", "search_tags": ["old"]},
        ),
        "gone-id": VectorRecordSnapshot("gone-id", "viking://resources/repo/gone.py", "gone.py", 2),
        "ghost-id": VectorRecordSnapshot(
            "ghost-id", "viking://resources/repo/ghost.py", "ghost.py", 2
        ),
    }

    from openviking.storage.context_update_plan import build_context_update_plan

    plan = build_context_update_plan(
        root_uri="viking://resources/repo",
        context_type="resource",
        request=RequestIntent("viking://resources/repo", "semantic_and_vectors"),
        diff=diff,
        new_kinds={"": "directory", "changed.py": "file"},
        artifact_paths={"changed.py": "repository/changed.py"},
        records=inventory,
        is_code_repo=True,
        account_id="acc",
    )

    assert isinstance(plan, ContextUpdatePlan)
    assert [(a.operation.value, a.relative_path) for a in plan.content_tree_actions] == [
        ("upsert", "changed.py"),
        ("delete", "gone.py"),
    ]
    assert {(a.operation.value, a.record_id) for a in plan.direct_index_actions} == {
        ("delete", "gone-id"),
        ("delete", "ghost-id"),
    }
    changed = next(e for e in plan.semantic_plan.tree.entries if e.relative_path == "changed.py")
    assert changed.semantic_action.value == "generate"
    assert changed.index_slots[0].operation == IndexOperation.UPSERT
    assert changed.index_slots[0].record_id == "changed-id"
    assert changed.index_slots[0].trigger.value == "output_changed"


def test_level_conflict_deletes_invalid_record_and_rebuilds_valid_slots():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "semantic_and_vectors"),
        diff=ResourceDiffResult(
            {
                "module": ResourceDiffEntry(
                    "module",
                    ContentState.UNCHANGED,
                    IndexState.LEVEL_CONFLICT,
                    old_kind="directory",
                    new_kind="directory",
                )
            }
        ),
        new_kinds={"": "directory", "module": "directory"},
        artifact_paths={},
        records={"stale-l2": VectorRecordSnapshot("stale-l2", f"{root}/module", "module", 2)},
        is_code_repo=False,
        account_id="acc",
    )

    assert [(a.operation.value, a.record_id) for a in plan.direct_index_actions] == [
        ("delete", "stale-l2")
    ]
    entry = next(
        entry for entry in plan.semantic_plan.tree.entries if entry.relative_path == "module"
    )
    assert entry.semantic_action.value == "aggregate"
    assert [(slot.level, slot.operation.value) for slot in entry.index_slots] == [
        (0, "upsert"),
        (1, "upsert"),
    ]


def test_builder_deletes_duplicate_same_level_records_without_id_conflict():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    records = {
        record_id: VectorRecordSnapshot(
            record_id,
            root + "/a.py",
            "a.py",
            2,
            {"md5": "old", "abstract": "old"},
        )
        for record_id in ("a-primary", "a-duplicate")
    }
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "semantic_and_vectors"),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.MODIFIED,
                    IndexState.STALE,
                    old_kind="file",
                    new_kind="file",
                    md5="new",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records=records,
        is_code_repo=True,
        account_id="acc",
    )

    slot = next(
        entry for entry in plan.semantic_plan.tree.entries if entry.relative_path == "a.py"
    ).slot(2)
    assert slot.record_id in records
    assert [(action.operation.value, action.record_id) for action in plan.direct_index_actions] == [
        ("delete", ({"a-primary", "a-duplicate"} - {slot.record_id}).pop())
    ]


def test_healthy_noop_produces_no_actions_or_semantic_plan():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    records = {
        "root-l0": VectorRecordSnapshot("root-l0", root, "", 0, {"abstract": "root abstract"}),
        "root-l1": VectorRecordSnapshot("root-l1", root, "", 1, {"abstract": "root overview"}),
        "a-l2": VectorRecordSnapshot(
            "a-l2",
            root + "/a.py",
            "a.py",
            2,
            {"md5": "same", "abstract": "a summary"},
        ),
    }
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "semantic_and_vectors"),
        diff=ResourceDiffResult(
            {
                "": ResourceDiffEntry(
                    "",
                    ContentState.UNCHANGED,
                    IndexState.COMPLETE,
                    old_kind="directory",
                    new_kind="directory",
                ),
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.UNCHANGED,
                    IndexState.COMPLETE,
                    old_kind="file",
                    new_kind="file",
                    md5="same",
                ),
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records=records,
        is_code_repo=True,
        account_id="acc",
    )

    assert plan.is_noop()
    assert plan.content_tree_actions == ()
    assert plan.semantic_plan is None
    assert plan.direct_index_actions == ()


def test_restore_with_matching_index_reuses_file_summary_and_aggregates_parent():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "semantic_and_vectors"),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.RESTORE,
                    IndexState.COMPLETE,
                    new_kind="file",
                    md5="same",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={
            "a-l2": VectorRecordSnapshot(
                "a-l2",
                root + "/a.py",
                "a.py",
                2,
                {"md5": "same", "abstract": "old summary"},
            ),
            "root-l0": VectorRecordSnapshot("root-l0", root, "", 0, {"abstract": "old root"}),
            "root-l1": VectorRecordSnapshot("root-l1", root, "", 1, {"abstract": "old overview"}),
        },
        is_code_repo=True,
        account_id="acc",
    )

    entries = {entry.relative_path: entry for entry in plan.semantic_plan.tree.entries}
    assert entries["a.py"].semantic_action.value == "reuse"
    assert entries[""].semantic_action.value == "aggregate"
    assert entries[""].membership_changed is True


def test_builder_keeps_scalar_intent_as_fallback_for_semantic_upsert():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, ScalarIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    record = VectorRecordSnapshot(
        "id-a",
        root + "/a.py",
        "a.py",
        2,
        {"md5": "old", "abstract": "old abstract", "search_tags": ["scope=old"]},
    )
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(
            root,
            "semantic_and_vectors",
            scalar_intents=(ScalarIntent("search_tags", "replace", ("scope=new",)),),
        ),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.MODIFIED,
                    IndexState.STALE,
                    old_kind="file",
                    new_kind="file",
                    md5="new",
                ),
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={"id-a": record},
        is_code_repo=True,
        account_id="acc",
    )
    slot = next(
        e for e in plan.semantic_plan.tree.entries if e.relative_path == "a.py"
    ).index_slots[0]
    assert slot.fields == {"search_tags": ["scope=new"]}
    assert slot.trigger.value == "output_changed"
    assert slot.fallback_update_fields is True
    assert plan.semantic_plan.ingest_options.search_tags is None


def test_builder_routes_scalar_update_directly_when_semantics_do_not_vectorize():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import (
        RequestIntent,
        ScalarIntent,
        VectorRecordSnapshot,
    )

    root = "viking://resources/repo"
    record = VectorRecordSnapshot(
        "a-l2",
        root + "/a.py",
        "a.py",
        2,
        {"abstract": "old", "search_tags": ["scope=old"]},
    )
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(
            root,
            "semantic_and_vectors",
            vectorize=False,
            scalar_intents=(ScalarIntent("search_tags", "replace", ("scope=new",)),),
        ),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.MODIFIED,
                    IndexState.STALE,
                    old_kind="file",
                    new_kind="file",
                    md5="new",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={record.record_id: record},
        is_code_repo=True,
        account_id="acc",
    )

    assert plan.semantic_plan is not None
    assert [
        (action.operation.value, action.record_id, dict(action.fields))
        for action in plan.direct_index_actions
    ] == [("update_fields", "a-l2", {"search_tags": ["scope=new"]})]


def test_vectors_only_upsert_preserves_existing_custom_scalars():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    record = VectorRecordSnapshot(
        "external-id",
        root + "/a.py",
        "a.py",
        2,
        {
            "abstract": "old",
            "md5": "old",
            "business_priority": 7,
            "vector": [1.0],
        },
    )
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "vectors_only"),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.MODIFIED,
                    IndexState.STALE,
                    old_kind="file",
                    new_kind="file",
                    md5="new",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={record.record_id: record},
        is_code_repo=False,
        account_id="acc",
    )

    action = plan.direct_index_actions[0]
    assert action.operation.value == "upsert"
    assert action.record_id == "external-id"
    assert action.fields["business_priority"] == 7
    assert "vector" not in action.fields


def test_vectors_only_restore_with_complete_index_does_not_reembed():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    record = VectorRecordSnapshot(
        "existing-id",
        root + "/a.py",
        "a.py",
        2,
        {"md5": "same", "abstract": "old", "search_tags": ["scope=old"]},
    )
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "vectors_only"),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.RESTORE,
                    IndexState.COMPLETE,
                    new_kind="file",
                    md5="same",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={record.record_id: record},
        is_code_repo=False,
        account_id="acc",
    )

    assert [
        (action.operation.value, action.relative_path) for action in plan.content_tree_actions
    ] == [("upsert", "a.py")]
    assert plan.semantic_plan is None
    assert plan.direct_index_actions == ()


def test_vectors_only_unchanged_file_with_extra_level_only_deletes_conflict():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "vectors_only"),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.UNCHANGED,
                    IndexState.LEVEL_CONFLICT,
                    old_kind="file",
                    new_kind="file",
                    md5="same",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={
            "valid-l2": VectorRecordSnapshot(
                "valid-l2", root + "/a.py", "a.py", 2, {"md5": "same"}
            ),
            "invalid-l0": VectorRecordSnapshot("invalid-l0", root + "/a.py", "a.py", 0, {}),
        },
        is_code_repo=False,
        account_id="acc",
    )

    assert [(action.operation.value, action.record_id) for action in plan.direct_index_actions] == [
        ("delete", "invalid-l0")
    ]


def test_builder_carries_request_ingest_options_into_semantic_plan():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent
    from openviking.utils.ingest_options import IngestOptions

    root = "viking://resources/repo"
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "semantic_and_vectors"),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.ADDED,
                    IndexState.MISSING,
                    new_kind="file",
                    md5="new",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={},
        is_code_repo=False,
        account_id="acc",
        ingest_options=IngestOptions.from_search_tags(["team=search"], mode="append"),
    )

    assert plan.semantic_plan.ingest_options.search_tags == ["team=search"]
    assert plan.semantic_plan.ingest_options.search_tag_mode == "append"


def test_builder_backfills_missing_md5_and_merges_scalar_update():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import (
        RequestIntent,
        ScalarIntent,
        VectorRecordSnapshot,
    )

    root = "viking://resources/repo"
    record = VectorRecordSnapshot(
        "a-l2",
        root + "/a.py",
        "a.py",
        2,
        {
            "md5": "",
            "abstract": "old",
            "search_tags": ["scope=old"],
        },
    )
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(
            root,
            "semantic_and_vectors",
            scalar_intents=(ScalarIntent("search_tags", "replace", ("scope=new",)),),
        ),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.UNCHANGED,
                    IndexState.STALE,
                    old_kind="file",
                    new_kind="file",
                    md5="resolved-md5",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={"a-l2": record},
        is_code_repo=True,
        account_id="acc",
    )

    assert plan.semantic_plan is None
    assert len(plan.direct_index_actions) == 1
    action = plan.direct_index_actions[0]
    assert action.record_id == "a-l2"
    assert action.operation.value == "update_fields"
    assert action.fields == {
        "md5": "resolved-md5",
        "search_tags": ["scope=new"],
    }


def test_builder_preserves_existing_record_identity_and_custom_scalars_on_upsert():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        build_context_update_plan,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import RequestIntent, VectorRecordSnapshot

    root = "viking://resources/repo"
    record = VectorRecordSnapshot(
        "external-record-id",
        root + "/a.py",
        "a.py",
        2,
        {
            "abstract": "old",
            "business_priority": 7,
            "vector": [1.0],
            "content": "large old body",
        },
    )
    plan = build_context_update_plan(
        root_uri=root,
        context_type="resource",
        request=RequestIntent(root, "semantic_and_vectors"),
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.MODIFIED,
                    IndexState.STALE,
                    old_kind="file",
                    new_kind="file",
                    md5="new",
                )
            }
        ),
        new_kinds={"": "directory", "a.py": "file"},
        artifact_paths={"a.py": "repository/a.py"},
        records={record.record_id: record},
        is_code_repo=True,
        account_id="acc",
    )

    slot = next(
        entry for entry in plan.semantic_plan.tree.entries if entry.relative_path == "a.py"
    ).slot(2)
    assert slot.record_id == "external-record-id"
    assert slot.scalar_override()["business_priority"] == 7
    assert "vector" not in slot.scalar_override()
    assert "content" not in slot.scalar_override()


@pytest.mark.asyncio
async def test_snapshot_builder_returns_one_canonical_context_plan():
    from openviking.storage.context_update_plan import (
        ContentState,
        build_context_update_plan_from_snapshot,
    )
    from openviking.storage.resource_rnfv import (
        FormalTreeSnapshot,
        NewArtifactSnapshot,
        RequestIntent,
        RNFVSnapshot,
        VectorIndexSnapshot,
    )
    from openviking.storage.viking_fs._diff_plan import NewEntry

    root = "viking://resources/repo"
    snapshot = RNFVSnapshot(
        request=RequestIntent(root, "semantic_and_vectors"),
        new=NewArtifactSnapshot({"a.py": NewEntry(md5="new")}),
        formal=FormalTreeSnapshot({}),
        vectors=VectorIndexSnapshot({}, frozenset({"id", "uri", "level", "md5"})),
    )
    diff, plan = await build_context_update_plan_from_snapshot(
        snapshot=snapshot,
        store=AsyncMock(),
        artifact_ref=object(),
        target=AsyncMock(),
        vikingdb=AsyncMock(),
        context_type="resource",
        is_code_repo=True,
        account_id="acc",
        ctx=object(),
        root_preexisting=False,
        artifact_paths={"a.py": "repository/a.py"},
    )

    assert diff.entries["a.py"].content_state is ContentState.ADDED
    assert [a.relative_path for a in plan.content_tree_actions] == ["", "a.py"]
    assert plan.content_tree_actions[1].artifact_path == "repository/a.py"
    assert plan.semantic_plan is not None
    entries = {entry.relative_path: entry for entry in plan.semantic_plan.tree.entries}
    assert entries[""].semantic_action.value == "aggregate"
    assert entries["a.py"].semantic_action.value == "generate"


@pytest.mark.asyncio
async def test_snapshot_builder_hydrates_scalars_for_vectors_only_upsert():
    from openviking.storage.context_update_plan import build_context_update_plan_from_snapshot
    from openviking.storage.resource_rnfv import (
        FormalTreeSnapshot,
        NewArtifactSnapshot,
        RequestIntent,
        RNFVSnapshot,
        VectorIndexSnapshot,
        VectorRecordSnapshot,
    )
    from openviking.storage.viking_fs._diff_plan import NewEntry, TargetFile

    root = "viking://resources/repo"
    record = VectorRecordSnapshot(
        "existing-id",
        root + "/a.py",
        "a.py",
        2,
        {"md5": "old", "abstract": "old summary"},
    )
    snapshot = RNFVSnapshot(
        request=RequestIntent(root, "vectors_only"),
        new=NewArtifactSnapshot({"a.py": NewEntry(md5="new")}),
        formal=FormalTreeSnapshot({"a.py": TargetFile()}),
        vectors=VectorIndexSnapshot(
            {record.record_id: record},
            frozenset({"id", "uri", "level", "md5", "abstract"}),
        ),
    )
    vikingdb = AsyncMock()
    vikingdb.hydrate_incremental_records.return_value = {
        record.record_id: {
            "id": record.record_id,
            "uri": record.uri,
            "level": 2,
            "md5": "old",
            "abstract": "old summary",
            "business_priority": 7,
        }
    }

    _, plan = await build_context_update_plan_from_snapshot(
        snapshot=snapshot,
        store=AsyncMock(),
        artifact_ref=object(),
        target=AsyncMock(),
        vikingdb=vikingdb,
        context_type="resource",
        is_code_repo=False,
        account_id="acc",
        ctx=object(),
        root_preexisting=True,
        artifact_paths={"a.py": "repository/a.py"},
    )

    vikingdb.hydrate_incremental_records.assert_awaited_once()
    assert plan.direct_index_actions[0].record_id == record.record_id
    assert plan.direct_index_actions[0].fields["business_priority"] == 7


@pytest.mark.asyncio
async def test_hydration_promotes_a_dependency_when_its_record_disappears():
    from openviking.storage.context_update_plan import (
        ContentState,
        IndexState,
        hydrate_context_plan_records,
    )
    from openviking.storage.resource_diff import ResourceDiffEntry, ResourceDiffResult
    from openviking.storage.resource_rnfv import VectorRecordSnapshot

    root = "viking://resources/repo"
    inventory = {
        "changed": VectorRecordSnapshot("changed", f"{root}/a.py", "a.py", 2, {"md5": "old"}),
        "sibling": VectorRecordSnapshot("sibling", f"{root}/b.py", "b.py", 2, {"md5": "same"}),
    }
    vikingdb = AsyncMock()
    vikingdb.hydrate_incremental_records.return_value = {
        "changed": {
            "id": "changed",
            "uri": f"{root}/a.py",
            "level": 2,
            "abstract": "old a",
        }
    }
    records, (active, _, retained) = await hydrate_context_plan_records(
        diff=ResourceDiffResult(
            {
                "a.py": ResourceDiffEntry(
                    "a.py",
                    ContentState.MODIFIED,
                    IndexState.STALE,
                    old_kind="file",
                    new_kind="file",
                    md5="new",
                ),
                "b.py": ResourceDiffEntry(
                    "b.py",
                    ContentState.UNCHANGED,
                    IndexState.COMPLETE,
                    old_kind="file",
                    new_kind="file",
                    md5="same",
                ),
            }
        ),
        new_kinds={"": "directory", "a.py": "file", "b.py": "file"},
        inventory=inventory,
        vikingdb=vikingdb,
        ctx=object(),
    )

    assert records["sibling"].record_id == "sibling"
    assert "b.py" in active
    assert "b.py" in retained


@pytest.mark.asyncio
async def test_content_executor_uploads_files_with_bounded_concurrency():
    from openviking.storage.context_update_plan import (
        ContentTreeAction,
        execute_content_tree_actions,
    )

    active = peak = 0
    gate = asyncio.Event()

    class Store:
        async def read_bytes(self, ref, path):
            return path.encode()

    class Target:
        async def write_file(self, path, data):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                gate.set()
            try:
                await asyncio.wait_for(gate.wait(), 1)
            finally:
                active -= 1

        async def delete_file(self, path):
            return None

        async def mkdir(self, path):
            return None

    actions = tuple(
        ContentTreeAction(
            "upsert",
            f"{index}.py",
            new_kind="file",
            artifact_path=f"{index}.py",
            md5=str(index),
        )
        for index in range(8)
    )
    await execute_content_tree_actions(
        actions,
        store=Store(),
        artifact_ref=object(),
        target=Target(),
        concurrency=3,
    )

    assert peak == 3


@pytest.mark.asyncio
async def test_resource_processor_dispatches_direct_index_actions_without_semantic(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.context_update_plan import IndexAction
    from openviking.utils.resource_processor import ResourceProcessor
    from openviking_cli.session.user_id import UserIdentifier

    enqueued = []

    async def enqueue(queue, message, **kwargs):
        del queue, kwargs
        enqueued.append(message)
        return True

    queue_manager = SimpleNamespace(
        EMBEDDING="embedding",
        get_queue=lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr("openviking.storage.queuefs.get_queue_manager", lambda: queue_manager)
    monkeypatch.setattr("openviking.utils.embedding_utils._enqueue_embedding_message", enqueue)
    processor = ResourceProcessor(SimpleNamespace(get_embedder=lambda: None))
    processor._vectorize_resource_file = AsyncMock()
    ctx = RequestContext(UserIdentifier("acc", "user"), Role.USER)
    await processor._enqueue_index_actions(
        (
            IndexAction("delete", "viking://resources/repo/a.py", 2, "id-a"),
            IndexAction(
                "update_fields",
                "viking://resources/repo/b.py",
                2,
                "id-b",
                fields={"search_tags": ["scope=new"]},
            ),
            IndexAction(
                "upsert",
                "viking://resources/repo/c.py",
                2,
                "id-c",
                fields={"search_tags": ["scope=new"]},
                md5="new-md5",
            ),
        ),
        ctx=ctx,
    )
    assert [msg.operation.value for msg in enqueued] == ["delete", "update_fields"]
    assert enqueued[0].record_ids == ["id-a"]
    assert enqueued[1].update_fields["search_tags"] == ["scope=new"]
    processor._vectorize_resource_file.assert_awaited_once_with(
        "viking://resources/repo/c.py",
        ctx=ctx,
        file_md5="new-md5",
        scalar_override={"search_tags": ["scope=new"], "_record_id": "id-c"},
        partial_update=False,
    )


def test_semantic_message_roundtrip_uses_explicit_plan():
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )
    from openviking.storage.queuefs.semantic_msg import SemanticMsg

    plan = SemanticPlan(
        "viking://resources/repo",
        "resource",
        tree=SemanticTreeSnapshot(
            (
                SemanticTreeEntry("", "directory", "unchanged", "aggregate"),
                SemanticTreeEntry(
                    "a.py",
                    "file",
                    "modified",
                    "generate",
                    md5="new",
                    index_slots=(
                        IndexSlot(
                            2,
                            "actual-id",
                            {"abstract": "old"},
                            operation="upsert",
                            trigger="output_changed",
                        ),
                    ),
                ),
            )
        ),
    )
    msg = SemanticMsg(uri=plan.root_uri, context_type="resource", plan=plan)
    restored = SemanticMsg.from_json(msg.to_json())
    assert restored.plan == plan
    assert (
        next(entry for entry in restored.plan.tree.entries if entry.relative_path == "a.py")
        .index_slots[0]
        .record_id
        == "actual-id"
    )


@pytest.mark.asyncio
async def test_v3_dag_reuses_explicit_node_without_listing_its_subtree(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )
    from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor
    from openviking_cli.session.user_id import UserIdentifier

    root = "viking://resources/repo"
    calls = []

    class FS:
        _async_agfs = None

        async def ls(self, uri, **kwargs):
            calls.append(uri)
            raise AssertionError("v3 plan must not list the live tree")

        def _uri_to_path(self, uri, ctx=None):
            return uri

    fs = FS()
    fs._async_agfs = fs
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fs)
    processor = AsyncMock()
    plan = SemanticPlan(
        root,
        "resource",
        tree=SemanticTreeSnapshot(
            (
                SemanticTreeEntry(
                    "",
                    "directory",
                    "unchanged",
                    "reuse",
                    index_slots=(IndexSlot(0, "root-l0", {"abstract": "old root"}),),
                ),
            )
        ),
    )
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=RequestContext(UserIdentifier("acc", "user"), Role.USER),
        semantic_plan=plan,
    )
    await executor.run(root)
    assert calls == []
    assert executor.root_write_result.wrote is False


@pytest.mark.asyncio
async def test_directory_index_slots_choose_exact_levels(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.abstract_overview import AbstractOverviewWriteResult
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )
    from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor
    from openviking_cli.session.user_id import UserIdentifier

    root = "viking://resources/repo"
    fs = SimpleNamespace(
        _async_agfs=None,
        _uri_to_path=lambda uri, ctx=None: uri,
    )
    fs._async_agfs = fs
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_openviking_config",
        lambda: SimpleNamespace(semantic=SimpleNamespace(overview_sample_limit=32)),
    )

    class Processor:
        _generate_overview = AsyncMock(return_value="overview")
        _vectorize_directory = AsyncMock(return_value={1})

        @staticmethod
        def _normalize_overview_generation(overview):
            return overview, "abstract"

    processor = Processor()
    plan = SemanticPlan(
        root,
        "resource",
        SemanticTreeSnapshot(
            (
                SemanticTreeEntry(
                    "",
                    "directory",
                    "unchanged",
                    "aggregate",
                    index_slots=(
                        IndexSlot(0, "l0", {"abstract": "old"}),
                        IndexSlot(1, "l1", None, operation="upsert", trigger="output_ready"),
                    ),
                ),
            )
        ),
    )
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=RequestContext(UserIdentifier("acc", "user"), Role.USER),
        semantic_plan=plan,
    )
    executor._write_directory_semantics = AsyncMock(
        return_value=AbstractOverviewWriteResult(wrote=True, overview_body_changed=True)
    )

    await executor.run(root)

    kwargs = processor._vectorize_directory.await_args.kwargs
    assert kwargs["include_abstract"] is False
    assert kwargs["include_overview"] is True


@pytest.mark.asyncio
async def test_directory_output_unchanged_still_applies_planned_scalar_fields(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.abstract_overview import AbstractOverviewWriteResult
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )
    from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor
    from openviking_cli.session.user_id import UserIdentifier

    root = "viking://resources/repo"
    fs = SimpleNamespace(_async_agfs=None, _uri_to_path=lambda uri, ctx=None: uri)
    fs._async_agfs = fs
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_openviking_config",
        lambda: SimpleNamespace(semantic=SimpleNamespace(overview_sample_limit=32)),
    )

    class Processor:
        _generate_overview = AsyncMock(return_value="overview")
        _vectorize_directory = AsyncMock(return_value=set())
        _update_vector_fields = AsyncMock(return_value=True)

        @staticmethod
        def _normalize_overview_generation(overview):
            return overview, "abstract"

    processor = Processor()
    plan = SemanticPlan(
        root,
        "resource",
        SemanticTreeSnapshot(
            (
                SemanticTreeEntry(
                    "",
                    "directory",
                    "unchanged",
                    "aggregate",
                    index_slots=(
                        IndexSlot(
                            0,
                            "root-l0",
                            {"abstract": "abstract"},
                            operation="upsert",
                            trigger="output_changed",
                            fields={"search_tags": ["scope=new"]},
                            fallback_update_fields=True,
                        ),
                        IndexSlot(
                            1,
                            "root-l1",
                            None,
                            operation="upsert",
                            trigger="output_ready",
                        ),
                    ),
                ),
            )
        ),
    )
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=RequestContext(UserIdentifier("acc", "user"), Role.USER),
        semantic_plan=plan,
    )
    executor._write_directory_semantics = AsyncMock(
        return_value=AbstractOverviewWriteResult(wrote=True)
    )

    await executor.run(root)

    assert processor._vectorize_directory.await_args.kwargs["include_abstract"] is False
    assert processor._vectorize_directory.await_args.kwargs["include_overview"] is True
    processor._update_vector_fields.assert_awaited_once_with(
        record_id="root-l0",
        uri=root,
        level=0,
        fields={"search_tags": ["scope=new"]},
        ctx=executor._ctx,
    )


@pytest.mark.asyncio
async def test_code_summary_unchanged_updates_md5_without_reembedding(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.abstract_overview import AbstractOverviewWriteResult
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )
    from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor
    from openviking_cli.session.user_id import UserIdentifier

    root = "viking://resources/repo"
    fs = SimpleNamespace(
        _async_agfs=None,
        _uri_to_path=lambda uri, ctx=None: uri,
        read_file_bytes=AsyncMock(return_value=b"changed body"),
    )
    fs._async_agfs = fs
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_dag.get_openviking_config",
        lambda: SimpleNamespace(semantic=SimpleNamespace(overview_sample_limit=32)),
    )

    class Processor:
        _generate_single_file_summary = AsyncMock(return_value={"name": "a.py", "summary": "same"})
        _update_file_vector_fields = AsyncMock(return_value=True)
        _vectorize_single_file = AsyncMock(return_value=True)
        _generate_overview = AsyncMock(return_value="overview")
        _vectorize_directory = AsyncMock(return_value=set())

        @staticmethod
        def _normalize_overview_generation(overview):
            return overview, "abstract"

    processor = Processor()
    plan = SemanticPlan(
        root,
        "resource",
        SemanticTreeSnapshot(
            (
                SemanticTreeEntry(
                    "",
                    "directory",
                    "unchanged",
                    "aggregate",
                    index_slots=(IndexSlot(0, "root-l0", {"abstract": "old"}),),
                ),
                SemanticTreeEntry(
                    "a.py",
                    "file",
                    "modified",
                    "generate",
                    md5="new-md5",
                    index_slots=(
                        IndexSlot(
                            2,
                            "a-l2",
                            {"abstract": "same"},
                            operation="upsert",
                            trigger="output_changed",
                            fallback_update_fields=True,
                        ),
                    ),
                ),
            )
        ),
        file_vector_source="summary_when_available",
    )
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=RequestContext(UserIdentifier("acc", "user"), Role.USER),
        semantic_plan=plan,
    )
    executor._write_directory_semantics = AsyncMock(
        return_value=AbstractOverviewWriteResult(wrote=True)
    )

    await executor.run(root)

    processor._vectorize_single_file.assert_not_awaited()
    assert processor._update_file_vector_fields.await_args.kwargs["record_id"] == "a-l2"
    assert processor._update_file_vector_fields.await_args.kwargs["file_md5"] == "new-md5"


@pytest.mark.asyncio
async def test_direct_only_plan_skips_semantic_queue(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.context_update_plan import IndexAction
    from openviking.utils.resource_processor import ResourceProcessor
    from openviking_cli.session.user_id import UserIdentifier

    root = "viking://resources/repo"
    lock = {"lease_ref": "plan"}
    viking_fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
    )
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    processor = ResourceProcessor(SimpleNamespace(get_embedder=lambda: None))
    processor._enqueue_index_actions = AsyncMock()
    summarizer = SimpleNamespace(summarize=AsyncMock())
    processor._get_summarizer = lambda: summarizer
    action = IndexAction(
        "update_fields",
        root + "/a.py",
        2,
        "a-l2",
        fields={"search_tags": ["scope=new"]},
    )

    await processor.finish_prepared_resource(
        {
            "root_uri": root,
            "temp_uri": root,
            "source_committed": True,
            "target_preexisting": True,
            "root_is_file": False,
            "context_update_plan": {
                "root_uri": root,
                "context_type": "resource",
                "content_tree_actions": [],
                "semantic_plan": None,
                "direct_index_actions": [
                    {
                        "operation": action.operation.value,
                        "uri": action.uri,
                        "level": action.level,
                        "record_id": action.record_id,
                        "fields": dict(action.fields),
                        "md5": action.md5,
                    }
                ],
            },
        },
        ctx=RequestContext(UserIdentifier("acc", "user"), Role.USER),
        resource_lock=lock,
        build_index=True,
        processing_mode="semantic_and_vectors",
    )

    processor._enqueue_index_actions.assert_awaited_once()
    summarizer.summarize.assert_not_awaited()
    viking_fs._async_agfs.pathlock_release.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_vectors_only_context_plan_does_not_run_legacy_vectorization(monkeypatch):
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.context_update_plan import ContextUpdatePlan, IndexAction
    from openviking.utils.resource_processor import ResourceProcessor
    from openviking_cli.session.user_id import UserIdentifier

    root = "viking://resources/repo"
    lock = {"lease_ref": "plan"}
    viking_fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
    )
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    processor = ResourceProcessor(SimpleNamespace(get_embedder=lambda: None))
    processor._enqueue_index_actions = AsyncMock()
    processor._vectorize_prepared_files = AsyncMock(
        side_effect=AssertionError("legacy vectors_only path must not run")
    )
    action = IndexAction("upsert", root + "/a.py", 2, "a-l2", md5="m")
    plan = ContextUpdatePlan(root, "resource", direct_index_actions=(action,))

    await processor.finish_prepared_resource(
        {
            "root_uri": root,
            "temp_uri": root,
            "source_committed": True,
            "target_preexisting": True,
            "root_is_file": False,
            "context_update_plan": plan.to_dict(),
        },
        ctx=RequestContext(UserIdentifier("acc", "user"), Role.USER),
        resource_lock=lock,
        build_index=True,
        processing_mode="vectors_only",
    )

    processor._enqueue_index_actions.assert_awaited_once_with(
        plan.direct_index_actions, ctx=processor._enqueue_index_actions.await_args.kwargs["ctx"]
    )
    processor._vectorize_prepared_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_index_enqueue_failure_releases_lock():
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.context_update_plan import ContextUpdatePlan, IndexAction
    from openviking.utils.resource_processor import ResourceProcessor
    from openviking_cli.session.user_id import UserIdentifier

    root = "viking://resources/repo"
    lock = {"lease_ref": "plan"}
    viking_fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
    )
    processor = ResourceProcessor(SimpleNamespace(get_embedder=lambda: None))
    processor._enqueue_index_actions = AsyncMock(side_effect=RuntimeError("queue unavailable"))
    plan = ContextUpdatePlan(
        root,
        "resource",
        direct_index_actions=(IndexAction("delete", root + "/a.py", 2, "a-l2"),),
    )

    with pytest.raises(RuntimeError, match="queue unavailable"):
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs
            )
            await processor.finish_prepared_resource(
                {
                    "root_uri": root,
                    "temp_uri": root,
                    "source_committed": True,
                    "target_preexisting": True,
                    "root_is_file": False,
                    "context_update_plan": plan.to_dict(),
                },
                ctx=RequestContext(UserIdentifier("acc", "user"), Role.USER),
                resource_lock=lock,
                build_index=True,
            )

    viking_fs._async_agfs.pathlock_release.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_semantic_processor_runs_only_plan_execution_roots(monkeypatch):
    from openviking.storage.context_update_plan import (
        IndexSlot,
        SemanticPlan,
        SemanticTreeEntry,
        SemanticTreeSnapshot,
    )
    from openviking.storage.queuefs.semantic_msg import SemanticMsg
    from openviking.storage.queuefs.semantic_processor import SemanticProcessor

    calls = []

    class Executor:
        stale = True
        root_write_result = None

        def __init__(self, **kwargs):
            calls.append(("init", kwargs["semantic_plan"]))

        async def run(self, uri):
            calls.append(("run", uri))

        def get_stats(self):
            return SimpleNamespace()

    root = "viking://resources/repo"
    plan = SemanticPlan(
        root,
        "resource",
        SemanticTreeSnapshot(
            (
                SemanticTreeEntry(
                    "",
                    "directory",
                    "unchanged",
                    "aggregate",
                    index_slots=(IndexSlot(0, "root-l0", {"abstract": "root"}),),
                ),
                SemanticTreeEntry(
                    "src",
                    "directory",
                    "unchanged",
                    "aggregate",
                    index_slots=(IndexSlot(0, "src-l0", {"abstract": "old"}),),
                ),
                SemanticTreeEntry(
                    "src/a.py",
                    "file",
                    "modified",
                    "generate",
                    md5="new",
                    index_slots=(
                        IndexSlot(
                            2,
                            "a-l2",
                            {"abstract": "old a"},
                            operation="upsert",
                            trigger="output_changed",
                        ),
                    ),
                ),
                SemanticTreeEntry(
                    "docs",
                    "directory",
                    "unchanged",
                    "aggregate",
                    index_slots=(IndexSlot(0, "docs-l0", {"abstract": "old"}),),
                ),
                SemanticTreeEntry(
                    "docs/b.md",
                    "file",
                    "modified",
                    "generate",
                    md5="new",
                    index_slots=(
                        IndexSlot(
                            2,
                            "b-l2",
                            {"abstract": "old b"},
                            operation="upsert",
                            trigger="output_changed",
                        ),
                    ),
                ),
            )
        ),
    )
    fs = SimpleNamespace(exists=AsyncMock(return_value=True))
    monkeypatch.setattr("openviking.storage.queuefs.semantic_processor.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.SemanticDagExecutor", Executor
    )
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.SemanticLockScope.resolve",
        AsyncMock(return_value=SimpleNamespace(lock=None, close=AsyncMock())),
    )
    processor = SemanticProcessor()
    processor._cleanup_local_artifact = AsyncMock()
    msg = SemanticMsg(
        uri=root,
        context_type="resource",
        account_id="acc",
        user_id="user",
        role="user",
        plan=plan,
    )

    await processor.on_dequeue(msg.to_dict())

    assert [value for kind, value in calls if kind == "run"] == [root]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_process_resource_does_not_handoff_local_artifact(
    monkeypatch, tmp_path, cleanup_fails
):
    from openviking.parse.output import LocalParseOutputStore
    from openviking.server.identity import RequestContext, Role
    from openviking.storage.context_update_plan import ContextUpdatePlan
    from openviking.utils.resource_processor import ResourceProcessor
    from openviking_cli.session.user_id import UserIdentifier

    store = LocalParseOutputStore(str(tmp_path / "artifacts"))
    ref = await store.create_artifact()
    await store.write_bytes(ref, "repository/a.py", b"a")
    if cleanup_fails:
        store.cleanup = AsyncMock(side_effect=OSError("cleanup unavailable"))
    processor = ResourceProcessor(SimpleNamespace(get_embedder=lambda: None))
    processor._build_parse_output_store = lambda: store
    processor._get_media_processor = lambda: SimpleNamespace(
        process=AsyncMock(
            return_value=SimpleNamespace(
                temp_dir_path=ref.root,
                source_path="x",
                source_format="repository",
                meta={},
                warnings=[],
                artifact_ref=ref,
                ensure_artifact_ref=lambda: ref,
            )
        )
    )
    processor.tree_builder.finalize_from_temp = AsyncMock(
        return_value=SimpleNamespace(
            root=SimpleNamespace(
                uri="viking://resources/repo",
                temp_uri=ref.root + "/repository",
            ),
            _root_is_file=False,
        )
    )
    processor._commit_directory_artifact_with_plan = AsyncMock(
        return_value=ContextUpdatePlan("viking://resources/repo", "resource")
    )
    viking_fs = SimpleNamespace(
        exists=AsyncMock(return_value=False),
        _uri_to_path=lambda uri, ctx=None: uri,
        bind_request_context=lambda ctx: nullcontext(),
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
    )
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    processor.acquire_resource_lock = AsyncMock(return_value={"lease_ref": "x"})

    result = await processor.process_resource(
        path="x",
        ctx=RequestContext(UserIdentifier("acc", "user"), Role.USER),
        to="viking://resources/repo",
        defer_post_processing=True,
        build_index=True,
    )

    assert result["_post_process"]["artifact_ref"] is None
    assert "file_md5s" not in result["_post_process"]
    assert "file_abstracts" not in result["_post_process"]
    assert "artifact_files" not in result["_post_process"]
    artifact_path = tmp_path.joinpath("artifacts", ref.root.split("/")[-1])
    assert artifact_path.exists() is cleanup_fails
