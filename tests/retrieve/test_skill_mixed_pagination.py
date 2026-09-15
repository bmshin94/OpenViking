# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever, RetrieverMode
from openviking.retrieve.skill_results import SkillResultResolver
from openviking.storage.viking_fs._semantic import _SemanticMixin
from openviking_cli.retrieve.types import ContextType, TypedQuery
from tests.retrieve.test_skill_package_results import SKILLS, Files, PagedStore, ctx, row


def _mixed_records(kind, *, directories_only=False):
    prefix = "viking://resources" if kind == "resource" else "viking://user/user1/memories"
    skill_level = 0 if directories_only else 2
    # The first ten records collapse to nine result items. The next page
    # contains different layers of the same eight non-Skill directories.
    records = [row(f"{SKILLS}/a/{i}.md", 0.99 - i / 1000, skill_level) for i in range(2)]
    records += [row(f"{prefix}/r{i}", 0.95, 0, kind=kind) for i in range(8)]
    records += [row(f"{prefix}/r{i}", 0.8, 1, kind=kind) for i in range(8)]
    return records


async def _search(store, files, route, *, limit=10):
    if route == "filter":
        fs = SimpleNamespace(
            _get_vector_store=lambda: store,
            _ensure_retrieval_scope=AsyncMock(),
            stat=files.stat,
        )
        result = await _SemanticMixin._find_by_filter(
            fs,
            {"op": "must", "field": "search_tags", "conds": ["team=x"]},
            ctx(),
            [],
            limit,
        )
        return [*result.skills, *result.resources, *result.memories]
    result = await HierarchicalRetriever(store, None).retrieve(
        TypedQuery("hello", None, ""),
        ctx(),
        limit=limit,
        mode=RetrieverMode.THINKING if route == "thinking" else RetrieverMode.QUICK,
        level=[2] if route == "thinking" else None,
        skill_resolver=SkillResultResolver(files, ctx()),
    )
    return result.matched_contexts


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "route"), [("resource", "quick"), ("memory", "filter")])
async def test_mixed_pages_keep_result_merging_but_distinguish_stored_layers(kind, route):
    store = PagedStore(_mixed_records(kind))
    files = Files()
    matches = await _search(store, files, route)
    skill_hits = [item for item in matches if item.context_type == ContextType.SKILL]
    other_hits = [item for item in matches if item.context_type != ContextType.SKILL]
    prefix = "viking://resources" if kind == "resource" else "viking://user/user1/memories"
    suffix = "/.abstract.md" if route == "quick" else ""

    assert len(matches) == 9
    assert [(item.uri, item.level) for item in skill_hits] == [(f"{SKILLS}/a/0.md", 2)]
    assert [(item.uri, item.level) for item in other_hits] == [
        (f"{prefix}/r{i}{suffix}", 0) for i in range(8)
    ]
    assert all(item.score == (0.95 if route == "quick" else 0) for item in other_hits)
    assert [call["offset"] for call in store.calls] == [0, 10]
    assert files.stat_calls == [f"{SKILLS}/a"]


@pytest.mark.asyncio
async def test_thinking_global_pages_distinguish_non_skill_directory_layers():
    store = PagedStore(_mixed_records("resource", directories_only=True))
    # Requesting files leaves the directory pool insufficient, so the retriever
    # must finish its global pagination even though no files exist in this case.
    matches = await _search(store, Files(), "thinking")
    assert matches == []
    assert [call["offset"] for call in store.calls] == [0, 10]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["quick", "filter", "thinking"])
async def test_mixed_backend_repeated_page_still_reports_incomplete_results(route):
    class RepeatingStore(PagedStore):
        def _page(self, records, kwargs):
            return super()._page(records, {**kwargs, "offset": 0})

    store = RepeatingStore(_mixed_records("resource", directories_only=route == "thinking"))
    with pytest.raises(RuntimeError, match="pagination did not advance; results are incomplete"):
        await _search(store, Files(), route)
    assert [call["offset"] for call in store.calls] == [0, 10]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["quick", "filter"])
async def test_internal_backups_do_not_consume_skill_limit_or_merge_normal_same_name(route):
    normal_roots = [f"{SKILLS}/demo", "viking://user/user1/skills/demo"]
    backup_roots = [
        f"{SKILLS}/.demo.update-backup-123",
        "viking://user/user1/skills/.demo.update-backup-456",
    ]
    records = []
    for root in backup_roots:
        records.extend([row(root, 0.99, 0), row(root, 0.98, 1)])
        records.extend(row(f"{root}/reference/{i}.md", 0.9) for i in range(3))
    # Only the package root is excluded: a hidden attachment in a normal Skill
    # still identifies that Skill, and equal package names have distinct owners.
    hit_uris = [f"{normal_roots[0]}/.rules.md", f"{normal_roots[1]}/reference/.hidden.md"]
    records.extend(row(uri, 0.8 - i / 10) for i, uri in enumerate(hit_uris))
    store = PagedStore(records)
    files = Files()
    matches = await _search(store, files, route, limit=2)

    assert {item.uri for item in matches} == set(hit_uris)
    assert len(matches) == 2
    assert all(item.context_type == ContextType.SKILL and item.level == 2 for item in matches)
    assert files.stat_calls == normal_roots
    assert [call["offset"] for call in store.calls] == (
        [0, 10] if route == "quick" else [0, 2, 4, 6, 8, 10]
    )


@pytest.mark.asyncio
async def test_excluding_skill_backups_does_not_hide_resource_or_memory_paths():
    records = [
        row("viking://resources/.demo.update-backup-123/file.md", 0.9, kind="resource"),
        row("viking://user/user1/memories/.demo.update-backup-456/file.md", 0.8, kind="memory"),
        row(f"{SKILLS}/.demo.update-backup-789/file.md", 0.7),
    ]
    files = Files()
    matches = await _search(PagedStore(records), files, "quick")
    assert {item.uri for item in matches} == {item["uri"] for item in records[:2]}
    assert files.stat_calls == []
