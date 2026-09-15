# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Skill package grouping, original hit preservation and retrieval completeness."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever, RetrieverMode
from openviking.retrieve.skill_results import (
    SkillResultResolver,
    merge_skill_results,
    skill_root_uri,
)
from openviking.server.identity import RequestContext, Role
from openviking.storage.viking_fs._semantic import _SemanticMixin
from openviking_cli.exceptions import NotFoundError, PermissionDeniedError
from openviking_cli.retrieve.types import (
    ContextType,
    MatchedContext,
    QueryPlan,
    QueryResult,
    TypedQuery,
)
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config import RetrievalConfig

SKILLS = "viking://agent/skills"


def ctx():
    return RequestContext(user=UserIdentifier("acc1", "user1"), role=Role.USER)


def row(uri, score, level=2, abstract="file summary", kind="skill"):
    return {"uri": uri, "_score": score, "level": level, "abstract": abstract, "context_type": kind}


class Files:
    def __init__(self, denied=(), missing=()):
        self.denied = set(denied)
        self.missing = set(missing)
        self.stat_calls = []

    async def stat(self, uri, ctx=None, skip_count=False):
        assert skip_count
        self.stat_calls.append(uri)
        if uri in self.denied:
            raise PermissionDeniedError("root denied", resource=uri)
        if uri in self.missing:
            raise NotFoundError(uri)
        return {"isDir": True}

    async def abstract(self, *args, **kwargs):
        pytest.fail("Skill grouping must not fetch or substitute root metadata")

    async def read(self, *args, **kwargs):
        pytest.fail("Skill grouping must not build an additional content preview")


class PagedStore:
    collection_name = "context"

    def __init__(self, records=(), children=None):
        self.records = list(records)
        self.children = children or {}
        self.calls = []
        self.child_calls = []

    def _acl_enabled(self, ctx):
        return False

    async def collection_exists_bound(self):
        return True

    def _page(self, records, kwargs):
        levels = kwargs.get("level")
        targets = kwargs.get("target_directories")
        filtered = [
            dict(record)
            for record in records
            if (levels is None or record["level"] in levels)
            and (
                not targets
                or any(
                    record["uri"] == root or record["uri"].startswith(root + "/")
                    for root in targets
                )
            )
        ]
        filtered.sort(key=lambda item: item["_score"], reverse=True)
        offset = kwargs.get("offset", 0)
        return filtered[offset : offset + kwargs.get("limit", 10)]

    async def search_in_tenant(self, ctx, **kwargs):
        self.calls.append(kwargs)
        return self._page(self.records, kwargs)

    async def filter_in_tenant(self, ctx, **kwargs):
        self.calls.append(kwargs)
        return self._page(self.records, kwargs)

    async def search_children_in_tenant(self, ctx, parent_uri, **kwargs):
        self.child_calls.append({"parent_uri": parent_uri, **kwargs})
        return self._page(self.children.get(parent_uri, []), kwargs)


def query(target=SKILLS):
    return TypedQuery("backup recovery", ContextType.SKILL, "", target_directories=[target])


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (f"{SKILLS}/demo/reference/nested/SKILL.md", f"{SKILLS}/demo"),
        (f"{SKILLS}/demo/reference/.overview.md", f"{SKILLS}/demo"),
        ("viking://user/alice/skills/demo/scripts/run", "viking://user/alice/skills/demo"),
        ("viking://agent/agent1/skills/demo/ref.txt", "viking://agent/agent1/skills/demo"),
        (SKILLS, ""),
        ("viking://user/alice/skills/.abstract.md", ""),
        (f"{SKILLS}/demo/.abstract.md", f"{SKILLS}/demo"),
        ("viking://resources/example/skills/demo/SKILL.md", ""),
    ],
)
def test_skill_root_uses_namespace_boundary(uri, expected):
    assert skill_root_uri(uri) == expected


@pytest.mark.asyncio
async def test_skill_namespace_summaries_are_excluded_before_pagination_counts():
    records = [row(SKILLS, 1, 0, "skills namespace"), row(SKILLS, 0.99, 1, "skills overview")]
    records.extend(row(f"{SKILLS}/a/ref/{i}.md", 0.9) for i in range(8))
    records.append(row(f"{SKILLS}/b/ref.md", 0.8))
    store = PagedStore(records)
    files = Files()
    result = await HierarchicalRetriever(store, None).retrieve(
        query(),
        ctx(),
        limit=2,
        mode=RetrieverMode.QUICK,
        skill_resolver=SkillResultResolver(files, ctx()),
    )
    assert [skill_root_uri(item.uri) for item in result.matched_contexts] == [
        f"{SKILLS}/a",
        f"{SKILLS}/b",
    ]
    assert [call["offset"] for call in store.calls] == [0, 10]
    assert files.stat_calls == [f"{SKILLS}/a", f"{SKILLS}/b"]


@pytest.mark.asyncio
async def test_quick_fills_distinct_skills_beyond_fixed_overfetch():
    records = [row(f"{SKILLS}/a/ref/{i}.md", 0.99 - i / 1000) for i in range(120)]
    records.append(row(f"{SKILLS}/b/ref/backup.md", 0.5))
    store = PagedStore(records)
    files = Files()
    result = await HierarchicalRetriever(store, None).retrieve(
        query(),
        ctx(),
        limit=2,
        mode=RetrieverMode.QUICK,
        skill_resolver=SkillResultResolver(files, ctx()),
    )
    assert [skill_root_uri(item.uri) for item in result.matched_contexts] == [
        f"{SKILLS}/a",
        f"{SKILLS}/b",
    ]
    assert result.matched_contexts[0].uri == f"{SKILLS}/a/ref/0.md"
    assert result.matched_contexts[0].level == 2
    assert result.matched_contexts[0].abstract == "file summary"
    assert result.matched_contexts[1].score == 0.5
    assert [call["offset"] for call in store.calls] == list(range(0, 121, 10))
    assert files.stat_calls == [f"{SKILLS}/a", f"{SKILLS}/b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_passing_hits", [False, True])
async def test_quick_stops_when_sorted_page_crosses_threshold(has_passing_hits):
    records = [row(f"{SKILLS}/a/ref/{i}.md", 0.1) for i in range(1000)]
    if has_passing_hits:
        records[:9] = [row(f"{SKILLS}/a/ref/{i}.md", 0.9) for i in range(9)]
    store = PagedStore(records)
    result = await HierarchicalRetriever(store, None).retrieve(
        query(),
        ctx(),
        limit=10,
        mode=RetrieverMode.QUICK,
        score_threshold=0.8,
        skill_resolver=SkillResultResolver(Files(), ctx()),
    )
    assert [item.uri for item in result.matched_contexts] == (
        [f"{SKILLS}/a/ref/0.md"] if has_passing_hits else []
    )
    assert len(store.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("score_gte", [False, True])
async def test_quick_threshold_equality_controls_pagination(score_gte):
    records = [row(f"{SKILLS}/a/ref/{i}.md", 0.8) for i in range(10)]
    records.append(row(f"{SKILLS}/b/ref.md", 0.8))
    store = PagedStore(records)
    result = await HierarchicalRetriever(store, None).retrieve(
        query(),
        ctx(),
        limit=2,
        mode=RetrieverMode.QUICK,
        score_threshold=0.8,
        score_gte=score_gte,
        skill_resolver=SkillResultResolver(Files(), ctx()),
    )
    assert [item.uri for item in result.matched_contexts] == (
        [f"{SKILLS}/a/ref/0.md", f"{SKILLS}/b/ref.md"] if score_gte else []
    )
    assert [call["offset"] for call in store.calls] == ([0, 10] if score_gte else [0])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_score", [float("nan"), None])
async def test_quick_invalid_page_boundary_does_not_hide_later_matches(invalid_score):
    class InvalidScoreStore(PagedStore):
        def _page(self, records, kwargs):
            # A malformed boundary score cannot prove that the next page fails.
            offset = kwargs.get("offset", 0)
            return [dict(record) for record in records[offset : offset + kwargs["limit"]]]

    records = [row(f"{SKILLS}/a/ref/{i}.md", 0.9) for i in range(9)]
    records.extend([row(f"{SKILLS}/a/bad.md", invalid_score), row(f"{SKILLS}/b/ref.md", 0.85)])
    store = InvalidScoreStore(records)
    result = await HierarchicalRetriever(store, None).retrieve(
        query(),
        ctx(),
        limit=2,
        mode=RetrieverMode.QUICK,
        score_threshold=0.8,
        skill_resolver=SkillResultResolver(Files(), ctx()),
    )
    assert [item.uri for item in result.matched_contexts] == [
        f"{SKILLS}/a/ref/0.md",
        f"{SKILLS}/b/ref.md",
    ]
    assert [call["offset"] for call in store.calls] == [0, 10]


@pytest.mark.asyncio
async def test_quick_fills_after_root_denial_and_missing_root():
    records = [row(f"{SKILLS}/denied/ref/{i}.md", 0.99) for i in range(10)]
    records.extend(row(f"{SKILLS}/missing/ref/{i}.md", 0.9, abstract="") for i in range(10))
    records.extend([row(f"{SKILLS}/b/SKILL.md", 0.8), row(f"{SKILLS}/c/SKILL.md", 0.7)])
    files = Files(denied=[f"{SKILLS}/denied"], missing=[f"{SKILLS}/missing"])
    store = PagedStore(records)
    result = await HierarchicalRetriever(store, None).retrieve(
        query(),
        ctx(),
        limit=2,
        mode=RetrieverMode.QUICK,
        skill_resolver=SkillResultResolver(files, ctx()),
    )
    assert [item.uri for item in result.matched_contexts] == [
        f"{SKILLS}/b/SKILL.md",
        f"{SKILLS}/c/SKILL.md",
    ]
    assert [call["offset"] for call in store.calls] == [0, 10, 20]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "relative_uri", "score", "abstract"),
    [
        (0, "reference/.abstract.md", 0.92, "directory abstract"),
        (1, "reference/.overview.md", 0.95, "directory overview"),
        (2, "reference/SKILL.md", 0.8, "nested definition"),
    ],
)
async def test_scoped_hits_preserve_requested_level_and_do_not_use_outside_scores(
    level, relative_uri, score, abstract
):
    root = f"{SKILLS}/demo"
    store = PagedStore(
        [
            row(root, 1, 0, "name: wrong"),
            row(f"{root}/other.md", 0.99),
            row(f"{root}/reference", 0.92, 0, "directory abstract"),
            row(f"{root}/reference", 0.95, 1, "directory overview"),
            row(f"{root}/reference/SKILL.md", 0.8, abstract="nested definition"),
        ]
    )
    filters = {"op": "must", "field": "search_tags", "conds": ["team=infra"]}
    result = await HierarchicalRetriever(store, None).retrieve(
        query(f"{root}/reference"),
        ctx(),
        limit=2,
        mode=RetrieverMode.QUICK,
        level=[level],
        scope_dsl=filters,
        skill_resolver=SkillResultResolver(Files(), ctx()),
    )
    [matched] = result.matched_contexts
    assert matched.uri == f"{root}/{relative_uri}"
    assert matched.level == level
    assert matched.score == score
    assert matched.abstract == abstract
    assert store.calls[0]["extra_filter"] is filters


@pytest.mark.asyncio
async def test_grouping_returns_the_original_hit_without_rewriting_any_content():
    root = f"{SKILLS}/demo"
    files = Files()
    resolver = SkillResultResolver(files, ctx())
    cases = [
        (f"{root}/reference/.overview.md", 1, "overview text"),
        (f"{root}/guide.md", 2, "x" * 1500),
    ]
    for uri, level, abstract in cases:
        original = MatchedContext(uri, ContextType.SKILL, level, abstract, score=0.9)
        [result] = await resolver.resolve([original])
        assert result is original
        assert result.uri == uri
        assert result.level == level
        assert result.abstract == abstract
        assert result.score == 0.9
    assert files.stat_calls == [root]


@pytest.mark.asyncio
async def test_same_name_in_two_scopes_and_ties_are_stable():
    agent_root = f"{SKILLS}/demo"
    user_root = "viking://user/user1/skills/demo"
    matches = [
        MatchedContext(f"{agent_root}/z.md", ContextType.SKILL, abstract="z", score=0.9),
        MatchedContext(f"{user_root}/a.md", ContextType.SKILL, abstract="user", score=0.9),
        MatchedContext(f"{agent_root}/a.md", ContextType.SKILL, abstract="a", score=0.9),
    ]
    result = await SkillResultResolver(Files(), ctx()).resolve(matches)
    assert [item.uri for item in result] == [f"{agent_root}/a.md", f"{user_root}/a.md"]
    assert result[0] is matches[2]


def reranked_retriever(store):
    retriever = HierarchicalRetriever(
        store,
        None,
        retrieval_config=RetrievalConfig(score_propagation_alpha=0.5, hotness_alpha=0),
    )
    scores = {"root": 0.6, "good": 0.9, "bad": 0.1}
    retriever._rerank_client = SimpleNamespace(
        rerank_batch=lambda query, documents: [scores[document] for document in documents]
    )
    return retriever


@pytest.mark.asyncio
async def test_thinking_replenishes_global_page_after_level_and_rerank_threshold():
    roots = [row(f"{SKILLS}/s{i:02}", 1 - i / 100, 0, "root") for i in range(11)]
    children = {
        item["uri"]: [row(f"{item['uri']}/ref.md", 0.9, abstract="good" if i in (0, 10) else "bad")]
        for i, item in enumerate(roots)
    }
    store = PagedStore(roots, children)
    result = await reranked_retriever(store).retrieve(
        query(),
        ctx(),
        limit=2,
        mode=RetrieverMode.THINKING,
        level=[2],
        score_threshold=0.5,
        skill_resolver=SkillResultResolver(Files(), ctx()),
    )
    assert [item.uri for item in result.matched_contexts] == [
        f"{SKILLS}/s00/ref.md",
        f"{SKILLS}/s10/ref.md",
    ]
    assert all(item.score == pytest.approx(0.75) for item in result.matched_contexts)
    assert all(item.level == 2 for item in result.matched_contexts)
    assert [call["offset"] for call in store.calls] == [0, 10]


@pytest.mark.asyncio
async def test_thinking_replenishes_child_page_after_level_and_rerank_threshold():
    roots = [row(f"{SKILLS}/s{i:02}", 1 - i / 100, 0, "root") for i in range(21)]
    children = {
        SKILLS: roots,
        **{
            item["uri"]: [
                row(f"{item['uri']}/ref.md", 0.9, abstract="good" if i in (0, 20) else "bad")
            ]
            for i, item in enumerate(roots)
        },
    }
    store = PagedStore(children=children)
    result = await reranked_retriever(store).retrieve(
        query(),
        ctx(),
        limit=2,
        mode=RetrieverMode.THINKING,
        level=[2],
        score_threshold=0.5,
        skill_resolver=SkillResultResolver(Files(), ctx()),
    )
    assert [item.uri for item in result.matched_contexts] == [
        f"{SKILLS}/s00/ref.md",
        f"{SKILLS}/s20/ref.md",
    ]
    assert [
        call.get("offset", 0) for call in store.child_calls if call["parent_uri"] == SKILLS
    ] == [0, 20]


@pytest.mark.asyncio
async def test_thinking_keeps_legacy_l0_only_skill_searchable():
    store = PagedStore([row(f"{SKILLS}/legacy", 0.8, 0, "root")])
    result = await reranked_retriever(store).retrieve(
        query(),
        ctx(),
        limit=1,
        mode=RetrieverMode.THINKING,
        skill_resolver=SkillResultResolver(Files(), ctx()),
    )
    assert result.matched_contexts[0].uri == f"{SKILLS}/legacy/.abstract.md"
    assert result.matched_contexts[0].level == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [RetrieverMode.QUICK, RetrieverMode.THINKING])
async def test_non_skill_results_and_search_calls_are_unchanged(mode):
    records = [
        row("viking://resources/a.md", 0.8, kind="resource"),
        row("viking://user/user1/memories/a.md", 0.7, kind="memory"),
    ]
    tq = TypedQuery("hello", None, "")
    children = {
        "viking://resources": records[:1],
        "viking://user/user1": records[1:],
    }
    plain_store, grouped_store = PagedStore(records, children), PagedStore(records, children)
    plain = await HierarchicalRetriever(plain_store, None).retrieve(tq, ctx(), limit=2, mode=mode)
    files = Files()
    grouped = await HierarchicalRetriever(grouped_store, None).retrieve(
        tq,
        ctx(),
        limit=2,
        mode=mode,
        skill_resolver=SkillResultResolver(files, ctx()),
    )
    assert grouped.matched_contexts == plain.matched_contexts
    assert len(grouped.matched_contexts) == 2
    assert grouped_store.calls == plain_store.calls
    assert grouped_store.child_calls == plain_store.child_calls
    assert files.stat_calls == []
    assert merge_skill_results(list(reversed(plain.matched_contexts))) == list(
        reversed(plain.matched_contexts)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "group_skills", "include_skill"),
    [
        (ContextType.RESOURCE, False, False),
        (ContextType.RESOURCE, True, False),
        (ContextType.MEMORY, True, True),
    ],
)
async def test_non_skill_initial_directory_hits_keep_legacy_overwrite_order(
    kind, group_skills, include_skill
):
    target = (
        "viking://resources" if kind == ContextType.RESOURCE else "viking://user/user1/memories"
    )
    uri = f"{target}/demo"
    records = [row(uri, 0.9, 0, "L0", kind.value), row(uri, 0.5, 1, "L1", kind.value)]
    if include_skill:
        records.append(row(f"{SKILLS}/demo", 0.7, 0))
    store = PagedStore(records)
    files = Files()
    result = await HierarchicalRetriever(
        store, None, retrieval_config=RetrievalConfig(hotness_alpha=0)
    ).retrieve(
        TypedQuery("demo", None if include_skill else kind, "", target_directories=[]),
        ctx(),
        limit=10,
        mode=RetrieverMode.THINKING,
        level=[0, 1],
        skill_resolver=SkillResultResolver(files, ctx()) if group_skills else None,
    )
    [matched] = [item for item in result.matched_contexts if item.context_type == kind]
    assert (matched.uri, matched.level, matched.score, matched.abstract) == (
        f"{uri}/.overview.md",
        1,
        0.5,
        "L1",
    )
    assert all(root.startswith(SKILLS + "/") for root in files.stat_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("winning_level", [0, 1])
async def test_skill_initial_directory_hits_keep_highest_scored_level(winning_level):
    uri = f"{SKILLS}/demo"
    records = [row(uri, 0.9 if level == winning_level else 0.5, level) for level in (0, 1)]
    result = await HierarchicalRetriever(
        PagedStore(records), None, retrieval_config=RetrievalConfig(hotness_alpha=0)
    ).retrieve(
        query(),
        ctx(),
        limit=1,
        mode=RetrieverMode.THINKING,
        level=[0, 1],
        skill_resolver=SkillResultResolver(Files(), ctx()),
    )
    [matched] = result.matched_contexts
    suffix = ".abstract.md" if winning_level == 0 else ".overview.md"
    assert (matched.uri, matched.level, matched.score) == (f"{uri}/{suffix}", winning_level, 0.9)


@pytest.mark.asyncio
async def test_filter_only_find_fills_packages_and_keeps_zero_scores():
    records = [row(f"{SKILLS}/a/ref/{i}.md", 0.9) for i in range(9)]
    records.append(row(f"{SKILLS}/b/ref.md", 0.8))
    store = PagedStore(records)
    files = Files()
    fs = SimpleNamespace(
        _get_vector_store=lambda: store,
        _ensure_retrieval_scope=AsyncMock(),
        stat=files.stat,
    )
    result = await _SemanticMixin._find_by_filter(
        fs, {"op": "must", "field": "search_tags", "conds": ["team=x"]}, ctx(), [SKILLS], 2, [2]
    )
    assert [item.uri for item in result.skills] == [f"{SKILLS}/a/ref/0.md", f"{SKILLS}/b/ref.md"]
    assert all(item.score == 0 for item in result.skills)
    assert [call["offset"] for call in store.calls] == [0, 2, 4, 6, 8]


@pytest.mark.asyncio
async def test_filter_only_directory_hit_keeps_its_original_uri_and_zero_score():
    level = 1
    uri = f"{SKILLS}/demo/reference"
    store = PagedStore([row(uri, 0.9, level, "directory content")])
    files = Files()
    fs = SimpleNamespace(
        _get_vector_store=lambda: store,
        _ensure_retrieval_scope=AsyncMock(),
        stat=files.stat,
    )
    result = await _SemanticMixin._find_by_filter(
        fs, {"op": "must", "field": "search_tags", "conds": ["team=x"]}, ctx(), [SKILLS], 2, [level]
    )
    [matched] = result.skills
    assert matched.uri == uri
    assert matched.level == level
    assert matched.abstract == "directory content"
    assert matched.score == 0
    assert files.stat_calls == [f"{SKILLS}/demo"]


@pytest.mark.asyncio
async def test_search_merges_skills_across_queries_without_changing_other_buckets(monkeypatch):
    class Intent:
        def __init__(self, **kwargs):
            pass

        async def analyze(self, **kwargs):
            return QueryPlan(
                [TypedQuery("first", None, ""), TypedQuery("second", None, "")], "", ""
            )

    class Retriever:
        def __init__(self, **kwargs):
            pass

        async def retrieve(self, tq, **kwargs):
            score = 0.8 if tq.query == "first" else 0.9
            return QueryResult(
                tq,
                [
                    MatchedContext("viking://resources/a.md", ContextType.RESOURCE, score=0.7),
                    MatchedContext(
                        f"{SKILLS}/demo/{tq.query}.md",
                        ContextType.SKILL,
                        score=score,
                        abstract=f"Content from {tq.query}",
                    ),
                ],
                [],
            )

    monkeypatch.setattr("openviking.retrieve.intent_analyzer.IntentAnalyzer", Intent)
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.HierarchicalRetriever", Retriever
    )
    fs = SimpleNamespace(
        _ctx_or_default=lambda supplied: supplied,
        _ensure_retrieval_scope=AsyncMock(),
        _get_vector_store=lambda: object(),
        _get_embedder=lambda: object(),
        retrieval_config=RetrievalConfig(enable_intent=True),
        rerank_config=None,
    )
    result = await _SemanticMixin.search(
        fs, "backup", ctx=ctx(), limit=1, session_info={"latest_archive_overview": "recent context"}
    )
    assert len(result.resources) == 2
    assert len(result.skills) == 1
    assert result.skills[0].score == 0.9
    assert result.skills[0].uri == f"{SKILLS}/demo/second.md"
    assert result.skills[0].level == 2
    assert result.skills[0].abstract == "Content from second"


@pytest.mark.asyncio
async def test_thinking_continues_when_best_hit_improves_inside_same_skill():
    class IndexedStore(PagedStore):
        def _acl_enabled(self, ctx):
            return True

    root = f"{SKILLS}/demo"
    records = []

    def directory(uri, vector_score, rerank_score):
        for level in (0, 1):
            records.append(
                row(uri, vector_score - level * 0.001, level, abstract=str(rerank_score))
            )

    directory(root, 1.0, 0.1)
    parent = root
    for i, score in enumerate([0.3, 0.5, 0.7, 0.9, 0.99]):
        parent = f"{parent}/dir{i}"
        directory(parent, 0.5 - i * 0.02, score)
    target = f"{parent}/answer.md"
    records.append(row(target, 0.3, 2, abstract="1.0"))
    for i in range(12):
        distractor = f"{SKILLS}/distractor{i:02}"
        directory(distractor, 0.99 - i * 0.005, 0.01)
        records.append(row(f"{distractor}/file.md", 0.99 - i * 0.005, 2, abstract=".01"))
    children = {}
    for record in records:
        children.setdefault(record["uri"].rsplit("/", 1)[0], []).append(record)
    store = IndexedStore(records, children)
    retriever = HierarchicalRetriever(
        store,
        None,
        retrieval_config=RetrievalConfig(hotness_alpha=0, score_propagation_alpha=0.5),
    )
    retriever._rerank_client = SimpleNamespace(
        rerank_batch=lambda query, documents: [float(document) for document in documents]
    )
    result = await retriever.retrieve(
        query(SKILLS), ctx(), limit=1, skill_resolver=SkillResultResolver(Files(), ctx())
    )
    assert result.matched_contexts[0].uri == target
    assert result.matched_contexts[0].score == pytest.approx(0.925625)


@pytest.mark.asyncio
async def test_filter_pages_keep_one_order_through_the_collection_adapter(monkeypatch):
    from openviking.storage.vectordb_adapters.local_adapter import LocalCollectionAdapter
    from openviking.storage.viking_vector_index_backend import (
        VikingVectorIndexBackend,
        _SingleAccountBackend,
    )
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    class Collection:
        def __init__(self):
            self.calls = []
            self.records = [
                dict(row(f"{SKILLS}/{name}", 0), id=name, updated_at=i, vector=vector)
                for i, (name, vector) in enumerate(
                    [
                        ("a/1.md", [1.0, 0.0, 0.0, 0.0]),
                        ("a/2.md", [0.99, 0.1, 0.0, 0.0]),
                        ("b/1.md", [-1.0, 0.0, 0.0, 0.0]),
                    ]
                )
            ]

        def page(self, records, offset, limit):
            page = records[offset : offset + limit]
            self.calls.append([item["id"] for item in page])
            return SimpleNamespace(
                data=[SimpleNamespace(id=item["id"], score=0.0, fields=item) for item in page]
            )

        def search_by_vector(self, *, dense_vector, offset, limit, **kwargs):
            records = sorted(
                self.records,
                key=lambda item: (
                    -sum(a * b for a, b in zip(dense_vector, item["vector"], strict=True))
                ),
            )
            return self.page(records, offset, limit)

        def search_by_scalar(self, *, field, order, offset, limit, **kwargs):
            records = sorted(self.records, key=lambda item: item[field], reverse=order == "desc")
            return self.page(records, offset, limit)

    collection = Collection()
    adapter = LocalCollectionAdapter("context", "", "default")
    adapter._collection = collection
    account = _SingleAccountBackend(
        VectorDBBackendConfig(backend="local", dimension=4),
        ctx().account_id,
        shared_adapter=adapter,
    )
    store = object.__new__(VikingVectorIndexBackend)
    store.acl_manager = None
    store._get_backend_for_context = lambda _: account
    fs = SimpleNamespace(
        _get_vector_store=lambda: store, _ensure_retrieval_scope=AsyncMock(), stat=Files().stat
    )
    monkeypatch.setattr(
        "openviking.storage.vectordb_adapters.base.get_openviking_config",
        lambda: SimpleNamespace(embedding=SimpleNamespace(dimension=4)),
    )
    values = iter([1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(
        "openviking.storage.vectordb_adapters.base.random.uniform", lambda *args: next(values)
    )
    result = await _SemanticMixin._find_by_filter(
        fs, {"op": "must", "field": "search_tags", "conds": ["team=x"]}, ctx(), [SKILLS], 2, [2]
    )
    assert {skill_root_uri(hit.uri) for hit in result.skills} == {f"{SKILLS}/a", f"{SKILLS}/b"}
    assert collection.calls == [["a/1.md", "a/2.md"], ["b/1.md"]]
