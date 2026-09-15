# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Hierarchical retriever for OpenViking.

Implements directory-based hierarchical retrieval with recursive search
and rerank-based relevance scoring.
"""

import asyncio
import heapq
import logging
import math
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from openviking.core.context import ContextLevel
from openviking.core.retrieval_targets import default_target_directories
from openviking.models.embedder.base import EmbedResult, embed_compat
from openviking.models.rerank import RerankClient
from openviking.retrieve.memory_lifecycle import hotness_score
from openviking.retrieve.retrieval_stats import get_stats_collector
from openviking.retrieve.skill_results import (
    SkillResultResolver,
    candidate_key,
    pagination_key,
    skill_root_uri,
)
from openviking.server.identity import RequestContext
from openviking.storage.abstract_overview import AbstractOverviewFormatError, body_for_preview
from openviking.storage.expr import FilterExpr
from openviking.storage.vikingdb_manager import VikingDBManager, VikingDBManagerProxy
from openviking.telemetry import get_current_telemetry
from openviking.utils.tags import normalize_search_tags
from openviking.utils.time_utils import parse_iso_datetime
from openviking.utils.token_estimation import (
    estimate_text_tokens,
    truncate_text_to_token_budget,
)
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.retrieve.types import (
    ContextType,
    MatchedContext,
    QueryResult,
    TypedQuery,
)
from openviking_cli.utils.config import RerankConfig, RetrievalConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class RetrieverMode(str):
    THINKING = "thinking"
    QUICK = "quick"


class HierarchicalRetriever:
    """Hierarchical retriever with dense and sparse vector support."""

    MAX_CONVERGENCE_ROUNDS = 3  # Stop after multiple rounds with unchanged topk
    DIRECTORY_DOMINANCE_RATIO = 1.2  # Directory score must exceed max child score
    GLOBAL_SEARCH_TOPK = 10  # Global retrieval count (more candidates = better rerank precision)
    MAX_PARALLEL_CHILD_SEARCHES = 4  # Limit per-request fan-out against remote vector stores
    LEVEL_URI_SUFFIX = {0: ".abstract.md", 1: ".overview.md"}

    def __init__(
        self,
        storage: VikingDBManager,
        embedder: Optional[Any],
        rerank_config: Optional[RerankConfig] = None,
        retrieval_config: Optional[RetrievalConfig] = None,
    ):
        """Initialize hierarchical retriever with rerank_config.

        Args:
            storage: VikingVectorIndexBackend instance
            embedder: Embedder instance (supports dense/sparse/hybrid)
            rerank_config: Rerank configuration (optional, will fallback to vector search only)
            retrieval_config: Retrieval ranking configuration.
        """
        self.vector_store = storage
        self.embedder = embedder
        self.rerank_config = rerank_config
        self.rerank_max_input_tokens = rerank_config.max_input_tokens if rerank_config else 0
        self.retrieval_config = retrieval_config or RetrievalConfig()
        self.hotness_alpha = self.retrieval_config.hotness_alpha
        self.score_propagation_alpha = self.retrieval_config.score_propagation_alpha

        # Use rerank threshold if available, otherwise use a default
        self.threshold = rerank_config.threshold if rerank_config else 0

        # Initialize rerank client — all providers go through unified dispatch
        if rerank_config and rerank_config.is_available():
            self._rerank_client = RerankClient.from_config(rerank_config)
            provider = rerank_config._effective_provider()
            logger.info(
                f"[HierarchicalRetriever] Rerank enabled (provider={provider}), threshold={self.threshold}"
            )
        else:
            self._rerank_client = None
            logger.info(
                f"[HierarchicalRetriever] Rerank not configured, using vector search only with threshold={self.threshold}"
            )

    async def retrieve(
        self,
        query: TypedQuery,
        ctx: RequestContext,
        limit: int = 5,
        mode: Optional[RetrieverMode] = None,
        score_threshold: Optional[float] = None,
        score_gte: bool = False,
        scope_dsl: Optional[FilterExpr | Dict[str, Any]] = None,
        level: Optional[List[int]] = None,
        skill_resolver: Optional[SkillResultResolver] = None,
    ) -> QueryResult:
        """
        Execute hierarchical retrieval.

        Args:
            ctx: Request context used for tenant and permission filtering
            score_threshold: Custom score threshold (overrides config)
            score_gte: True uses >=, False uses >
            scope_dsl: Additional scope constraints passed from public find/search filter
            level: Optional result level filter (0=L0, 1=L1, 2=L2)
        """
        t0 = time.monotonic()
        telemetry = get_current_telemetry()
        effective_threshold = self._resolve_threshold(score_threshold)
        image_query = bool(getattr(query, "image_query", False))
        if mode is None:
            mode = RetrieverMode.QUICK if not self._rerank_client else RetrieverMode.THINKING
        if image_query:
            mode = RetrieverMode.QUICK
            if level is None:
                level = [2]

        # 创建 proxy 包装器，绑定当前 ctx
        vector_proxy = VikingDBManagerProxy(self.vector_store, ctx)

        target_dirs = [d for d in (query.target_directories or []) if d]

        if not await vector_proxy.collection_exists_bound():
            logger.warning(
                "[RecursiveSearch] Collection %s does not exist",
                vector_proxy.collection_name,
            )
            return QueryResult(
                query=query,
                matched_contexts=[],
                searched_directories=[],
            )

        # Generate query vectors once to avoid duplicate embedding calls
        query_vector = None
        sparse_query_vector = None
        if self.embedder:
            if image_query and not getattr(self.embedder, "supports_multimodal", False):
                raise InvalidArgumentError("Image search requires a multimodal embedding model.")
            with telemetry.measure("search.embed_query"):
                embedding_input = getattr(query, "embedding_input", None) or query.query
                result: EmbedResult = await embed_compat(
                    self.embedder,
                    embedding_input,
                    is_query=True,
                )
                query_vector = result.dense_vector
                sparse_query_vector = result.sparse_vector

        # Step 1: Determine starting directories based on explicit target dirs.
        if target_dirs:
            root_uris = target_dirs
        else:
            root_uris = default_target_directories(ctx, context_type=query.context_type)

        context_type = query.context_type.value if query.context_type else None
        if image_query and context_type is None:
            context_type = ContextType.RESOURCE.value

        if mode == RetrieverMode.QUICK:
            search_limit = (
                max(limit * 5, 50) if image_query else max(limit, self.GLOBAL_SEARCH_TOPK)
            )
            with telemetry.measure("search.vector_retrieval"):
                quick_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=level,
                    limit=search_limit,
                )
            telemetry.count("vector.searches", 1)
            telemetry.count("vector.scored", len(quick_results))
            telemetry.count("vector.scanned", len(quick_results))

            collected_by_uri: Dict[Any, Dict[str, Any]] = {}
            offset = 0
            has_skill = False
            seen_keys = set()
            while True:
                page_keys = {pagination_key(result) for result in quick_results}
                if offset and page_keys and page_keys <= seen_keys:
                    raise RuntimeError(
                        "Skill search pagination did not advance; results are incomplete"
                    )
                seen_keys.update(page_keys)
                for result in quick_results:
                    uri = result.get("uri", "")
                    if not uri:
                        continue
                    has_skill |= result.get("context_type") == ContextType.SKILL.value
                    score = self._finite_score(result.get("_score", 0.0))
                    if not self._passes_threshold(score, effective_threshold, score_gte):
                        continue
                    candidate = dict(result)
                    candidate["_score"] = score
                    candidate["_final_score"] = score
                    key = candidate_key(candidate) if skill_resolver else uri
                    previous = collected_by_uri.get(key)
                    if previous is None or score > previous.get("_final_score", 0.0):
                        collected_by_uri[key] = candidate
                if not skill_resolver or not has_skill or len(quick_results) < search_limit:
                    break
                # QUICK pages are ordered by vector score. Once their boundary
                # fails the threshold, later pages cannot add eligible hits.
                # An invalid score does not establish a safe stopping boundary.
                boundary_score = self._finite_score(
                    quick_results[-1].get("_score"), default=math.nan
                )
                if math.isfinite(boundary_score) and not self._passes_threshold(
                    boundary_score, effective_threshold, score_gte
                ):
                    break
                visible_matches = await self._convert_to_matched_contexts(
                    list(collected_by_uri.values()), ctx=ctx, apply_hotness=False
                )
                if len(await skill_resolver.resolve(visible_matches)) >= limit:
                    break
                offset += len(quick_results)
                quick_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=level,
                    limit=search_limit,
                    offset=offset,
                )
                telemetry.count("vector.searches", 1)
                telemetry.count("vector.scored", len(quick_results))
                telemetry.count("vector.scanned", len(quick_results))

            candidates = sorted(
                collected_by_uri.values(),
                key=lambda x: x.get("_final_score", 0.0),
                reverse=True,
            )
            apply_hotness = False
            rerank_used = False
        else:
            # Step 2: Global vector search to supplement starting points
            with telemetry.measure("search.vector_retrieval"):
                global_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=[0, 1],
                    limit=max(limit, self.GLOBAL_SEARCH_TOPK),
                )
            telemetry.count("vector.searches", 1)
            telemetry.count("vector.scored", len(global_results))
            telemetry.count("vector.scanned", len(global_results))

            leaf_results: List[Dict[str, Any]] = []
            if self.vector_store._acl_enabled(ctx) and (level is None or 2 in level):
                leaf_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=[2],
                    limit=max(limit, self.GLOBAL_SEARCH_TOPK),
                )
                telemetry.count("vector.searches", 1)
                telemetry.count("vector.scored", len(leaf_results))
                telemetry.count("vector.scanned", len(leaf_results))

                if self._rerank_client and mode == RetrieverMode.THINKING and leaf_results:
                    leaf_scores = await self._rerank_scores(
                        query.query,
                        [str(result.get("abstract", "")) for result in leaf_results],
                        [self._finite_score(result.get("_score", 0.0)) for result in leaf_results],
                    )
                    leaf_results = [
                        {**result, "_score": score}
                        for result, score in zip(leaf_results, leaf_scores, strict=True)
                    ]

            # Debug: Print all URIs in global_results
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[retrieve] target_dirs: {target_dirs}")
                logger.debug(f"[retrieve] root_uris: {root_uris}")
                logger.debug(f"[retrieve] scope_dsl: {scope_dsl}")
                logger.debug(
                    f"[retrieve] Step 2 completed, global_results contains {len(global_results)} items:"
                )
                for i, r in enumerate(global_results):
                    uri = r.get("uri", "UNKNOWN_URI")
                    score = r.get("_score", 0.0)
                    result_level = r.get("level", "UNKNOWN_LEVEL")
                    account_id = r.get("account_id", "UNKNOWN_ACCOUNT_ID")
                    logger.debug(
                        f"  [{i}] URI: {uri}, score: {score:.4f}, level: {result_level}, account_id: {account_id}"
                    )

            # Step 3: Pick recursive entry points from directory hits and explicit roots.
            directory_scores = [self._finite_score(r.get("_score", 0.0)) for r in global_results]
            if self._rerank_client and mode == RetrieverMode.THINKING:
                directory_scores = await self._rerank_scores(
                    query.query,
                    [str(r.get("abstract", "")) for r in global_results],
                    directory_scores,
                )

            starting_points = []
            seen_starting_uris = set()
            for result, score in zip(global_results, directory_scores, strict=True):
                uri = result.get("uri", "")
                if not uri or uri in seen_starting_uris:
                    continue
                starting_points.append((uri, score))
                seen_starting_uris.add(uri)

            for uri in root_uris:
                if uri not in seen_starting_uris:
                    starting_points.append((uri, 0.0))
                    seen_starting_uris.add(uri)

            # Add directory hits to the result pool only when explicitly requested.
            initial_candidates = list(leaf_results)
            for result, score in zip(global_results, directory_scores, strict=True):
                if level is None:
                    # Skill roots must remain searchable before old packages
                    # have been rebuilt with child records.
                    if not skill_resolver or result.get("context_type") != "skill":
                        continue
                elif result.get("level", 2) not in level:
                    continue
                candidate = dict(result)
                candidate["_score"] = score
                initial_candidates.append(candidate)

            # Step 4: Recursive search
            with telemetry.measure("search.vector_retrieval"):
                candidates = await self._recursive_search(
                    vector_proxy=vector_proxy,
                    query=query.query,
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    starting_points=starting_points,
                    limit=limit,
                    mode=mode,
                    threshold=effective_threshold,
                    score_gte=score_gte,
                    context_type=context_type,
                    target_dirs=target_dirs,
                    scope_dsl=scope_dsl,
                    initial_candidates=initial_candidates,
                    level=level,
                    skill_resolver=skill_resolver,
                )

            # Directory counts alone cannot establish that enough Skills match:
            # their files may fail the requested level, threshold or root ACL.
            # Resume global pages only after evaluating the completed results.
            if skill_resolver and any(
                item.get("context_type") == "skill"
                for item in [*global_results, *leaf_results, *candidates]
            ):
                page_size = max(limit, self.GLOBAL_SEARCH_TOPK)
                pending = [
                    (levels, len(page), {pagination_key(item) for item in page})
                    for levels, page in [([0, 1], global_results), ([2], leaf_results)]
                    if len(page) >= page_size
                ]
                while pending:
                    converted = await self._convert_to_matched_contexts(candidates, ctx=ctx)
                    if len(await skill_resolver.resolve(converted)) >= limit:
                        break
                    page_levels, offset, seen = pending.pop(0)
                    page = await vector_proxy.search_in_tenant(
                        query_vector=query_vector,
                        sparse_query_vector=sparse_query_vector,
                        context_type=context_type,
                        target_directories=target_dirs,
                        extra_filter=scope_dsl,
                        level=page_levels,
                        limit=page_size,
                        offset=offset,
                    )
                    telemetry.count("vector.searches", 1)
                    telemetry.count("vector.scored", len(page))
                    telemetry.count("vector.scanned", len(page))
                    keys = {pagination_key(item) for item in page}
                    if keys and keys <= seen:
                        raise RuntimeError(
                            "Skill search pagination did not advance; results are incomplete"
                        )
                    if len(page) >= page_size:
                        pending.append((page_levels, offset + len(page), seen | keys))
                    # Extra searches are only for Skill completeness. Do not
                    # expand resource/memory candidates beyond their old path.
                    skill_page = [
                        item
                        for item in page
                        if item.get("context_type") == "skill" and item.get("uri")
                    ]
                    if not skill_page:
                        continue
                    scores = [self._finite_score(item.get("_score", 0.0)) for item in skill_page]
                    if self._rerank_client and mode == RetrieverMode.THINKING:
                        scores = await self._rerank_scores(
                            query.query,
                            [str(item.get("abstract", "")) for item in skill_page],
                            scores,
                        )
                    more_starts = []
                    more_candidates = [
                        {**item, "_score": item.get("_final_score", item.get("_score", 0.0))}
                        for item in candidates
                    ]
                    for item, score in zip(skill_page, scores, strict=True):
                        if page_levels != [2] and item.get("uri") not in seen_starting_uris:
                            more_starts.append((item["uri"], score))
                            seen_starting_uris.add(item["uri"])
                        if level is None or item.get("level", 2) in level:
                            more_candidates.append({**item, "_score": score})
                    candidates = await self._recursive_search(
                        vector_proxy=vector_proxy,
                        query=query.query,
                        query_vector=query_vector,
                        sparse_query_vector=sparse_query_vector,
                        starting_points=more_starts,
                        limit=limit,
                        mode=mode,
                        threshold=effective_threshold,
                        score_gte=score_gte,
                        context_type=context_type,
                        target_dirs=target_dirs,
                        scope_dsl=scope_dsl,
                        initial_candidates=more_candidates,
                        level=level,
                        skill_resolver=skill_resolver,
                    )
            apply_hotness = True
            rerank_used = self._rerank_client is not None and mode == RetrieverMode.THINKING

        # Step 6: Convert results
        matched = await self._convert_to_matched_contexts(
            candidates,
            ctx=ctx,
            apply_hotness=apply_hotness,
        )
        if skill_resolver:
            matched = await skill_resolver.resolve(matched)
        final = matched[:limit]

        elapsed_ms = (time.monotonic() - t0) * 1000
        get_stats_collector().record_query(
            context_type=context_type or "unknown",
            result_count=len(final),
            scores=[m.score for m in final],
            latency_ms=elapsed_ms,
            rerank_used=rerank_used,
        )

        return QueryResult(
            query=query,
            matched_contexts=final,
            searched_directories=root_uris,
        )

    def _resolve_threshold(self, threshold: Optional[float]) -> float:
        resolved = threshold if threshold is not None else self.threshold
        return resolved if resolved is not None else 0.0

    @staticmethod
    def _finite_score(value: Any, default: float = 0.0) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return default
        return score if math.isfinite(score) else default

    @staticmethod
    def _passes_threshold(score: float, threshold: float, score_gte: bool) -> bool:
        if score_gte:
            return score >= threshold
        return score > threshold

    async def _rerank_scores(
        self,
        query: str,
        documents: List[str],
        fallback_scores: List[float],
    ) -> List[float]:
        """Return rerank scores or fall back to vector scores."""
        if not self._rerank_client or not documents:
            return fallback_scores

        rerank_query = query
        rerank_documents = [
            (index, document) for index, document in enumerate(documents) if document.strip()
        ]
        if not rerank_documents:
            return fallback_scores

        if self.rerank_max_input_tokens > 0:
            max_query_tokens = self.rerank_max_input_tokens * 3 // 4
            if estimate_text_tokens(query) > max_query_tokens:
                rerank_query = truncate_text_to_token_budget(query, max_query_tokens)
            document_tokens = self.rerank_max_input_tokens - estimate_text_tokens(rerank_query)
            rerank_documents = [
                (index, truncate_text_to_token_budget(document, document_tokens))
                for index, document in rerank_documents
            ]

        try:
            scores = await asyncio.to_thread(
                self._rerank_client.rerank_batch,
                rerank_query,
                [document for _, document in rerank_documents],
            )
        except Exception as e:
            logger.warning(
                "[HierarchicalRetriever] Rerank failed, fallback to vector scores: %s", e
            )
            return fallback_scores

        if not scores or len(scores) != len(rerank_documents):
            logger.warning(
                "[HierarchicalRetriever] Invalid rerank result, fallback to vector scores"
            )
            return fallback_scores

        normalized_scores = list(fallback_scores)
        for score, (index, _) in zip(scores, rerank_documents, strict=True):
            normalized_scores[index] = self._finite_score(score, fallback_scores[index])
        return normalized_scores

    async def _recursive_search(
        self,
        vector_proxy: VikingDBManagerProxy,
        query: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        starting_points: List[Tuple[str, float]],
        limit: int,
        mode: str,
        threshold: Optional[float] = None,
        score_gte: bool = False,
        context_type: Optional[str] = None,
        target_dirs: Optional[List[str]] = None,
        scope_dsl: Optional[FilterExpr | Dict[str, Any]] = None,
        initial_candidates: Optional[List[Dict[str, Any]]] = None,
        level: Optional[List[int]] = None,
        skill_resolver: Optional[SkillResultResolver] = None,
    ) -> List[Dict[str, Any]]:
        """
        Recursive search with directory priority return and score propagation.

        Args:
            threshold: Score threshold
            score_gte: True uses >=, False uses >
            grep_patterns: Keyword match patterns
            scope_dsl: Additional scope constraints from public find/search filter
        """
        effective_threshold = self._resolve_threshold(threshold)

        sparse_query_vector = sparse_query_vector or None

        collected_by_uri: Dict[Any, Dict[str, Any]] = {}
        dir_queue: List[tuple] = []  # Priority queue: (-score, uri)
        visited: set = set()
        prev_topk_state: set = set()
        prev_pool_size = 0
        convergence_rounds = 0
        stagnant_rounds = 0
        has_skill = False
        pending_pages: List[Tuple[str, float, int]] = []
        page_keys_by_uri: Dict[str, set] = {}
        current_topk_uris: set = set()
        page_size = max(limit * 2, 20)

        # Add initial candidates that match the requested level.
        if initial_candidates:
            for r in initial_candidates:
                uri = r.get("uri", "")
                if not uri:
                    continue
                if level is None or r.get("level", 2) in level:
                    score = self._finite_score(r.get("_score", 0.0))
                    if not self._passes_threshold(score, effective_threshold, score_gte):
                        logger.debug(
                            f"[RecursiveSearch] Initial candidate URI {uri} score {score:.4f} did not pass threshold {effective_threshold}"
                        )
                        continue
                    r["_final_score"] = score
                    if skill_resolver and r.get("context_type") == ContextType.SKILL.value:
                        key = candidate_key(r)
                        previous = collected_by_uri.get(key)
                        if previous is None or score > previous.get("_final_score", 0):
                            collected_by_uri[key] = r
                    else:
                        # Preserve the original overwrite order for other types.
                        collected_by_uri[uri] = r
                    has_skill |= r.get("context_type") == ContextType.SKILL.value
                    logger.debug(
                        f"[RecursiveSearch] Added initial candidate: {uri} (score: {score:.4f})"
                    )

        alpha = self.score_propagation_alpha

        # Initialize: process starting points
        for uri, score in starting_points:
            heapq.heappush(dir_queue, (-score, uri))

        async def search_children(current_uri: str, offset: int = 0) -> List[Dict[str, Any]]:
            paging = {"offset": offset} if offset else {}
            return await vector_proxy.search_children_in_tenant(
                parent_uri=current_uri,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,  # Pass sparse vector
                context_type=context_type,
                target_directories=target_dirs,
                extra_filter=scope_dsl,
                limit=page_size,
                **paging,
            )

        parallelism = max(1, self.MAX_PARALLEL_CHILD_SEARCHES)

        while dir_queue or (pending_pages and len(current_topk_uris) < limit):
            batch: List[Tuple[str, float, int]] = []
            while dir_queue and len(batch) < parallelism:
                temp_score, current_uri = heapq.heappop(dir_queue)
                current_score = -temp_score
                if current_uri in visited:
                    continue
                visited.add(current_uri)
                logger.info(f"[RecursiveSearch] Entering URI: {current_uri}")
                batch.append((current_uri, current_score, 0))

            if not batch and pending_pages and len(current_topk_uris) < limit:
                batch = pending_pages[:parallelism]
                del pending_pages[:parallelism]

            if not batch:
                continue

            batch_results = await asyncio.gather(
                *(search_children(current_uri, offset) for current_uri, _, offset in batch)
            )

            telemetry = get_current_telemetry()
            for (current_uri, current_score, offset), results in zip(
                batch, batch_results, strict=True
            ):
                telemetry.count("vector.searches", 1)
                telemetry.count("vector.scored", len(results))
                telemetry.count("vector.scanned", len(results))

                if not results:
                    continue

                if skill_resolver and (
                    offset or any(item.get("context_type") == "skill" for item in results)
                ):
                    keys = {pagination_key(item) for item in results}
                    seen = page_keys_by_uri.setdefault(current_uri, set())
                    if offset and keys <= seen:
                        raise RuntimeError(
                            "Skill search pagination did not advance; results are incomplete"
                        )
                    seen.update(keys)
                    if len(results) >= page_size:
                        pending_pages.append((current_uri, current_score, offset + len(results)))
                if offset:
                    results = [item for item in results if item.get("context_type") == "skill"]

                query_scores = [self._finite_score(r.get("_score", 0.0)) for r in results]
                if self._rerank_client and mode == RetrieverMode.THINKING:
                    documents = [str(r.get("abstract", "")) for r in results]
                    query_scores = await self._rerank_scores(query, documents, query_scores)

                for r, score in zip(results, query_scores, strict=True):
                    uri = r.get("uri", "")
                    has_skill |= r.get("context_type") == ContextType.SKILL.value
                    final_score = (
                        alpha * score + (1 - alpha) * current_score if current_score else score
                    )

                    if not self._passes_threshold(final_score, effective_threshold, score_gte):
                        logger.debug(
                            f"[RecursiveSearch] URI {uri} score {final_score} did not pass threshold {effective_threshold}"
                        )
                        continue

                    telemetry.count("vector.passed", 1)
                    if level is None or r.get("level", 2) in level:
                        # Deduplicate by URI and keep the highest-scored candidate.
                        key = candidate_key(r) if skill_resolver else uri
                        previous = collected_by_uri.get(key)
                        if previous is None or final_score > previous.get("_final_score", 0):
                            r["_final_score"] = final_score
                            collected_by_uri[key] = r
                            logger.debug(
                                "[RecursiveSearch] Updated URI: %s candidate score to %.4f",
                                uri,
                                final_score,
                            )

                    # Only recurse into directories (L0/L1). L2 files are terminal hits.
                    if uri not in visited and r.get("level", 2) != 2:
                        heapq.heappush(dir_queue, (-final_score, uri))

            # Convergence check after each parallel expansion round.
            if skill_resolver and has_skill:
                current_matches = await self._convert_to_matched_contexts(
                    list(collected_by_uri.values()), ctx=skill_resolver.ctx
                )
                grouped = await skill_resolver.resolve(current_matches)
                current_topk_uris = {
                    skill_root_uri(c.uri) if c.context_type == ContextType.SKILL else c.uri
                    for c in grouped[:limit]
                }
                current_topk_state = {
                    (c.uri, c.level, c.score) if c.context_type == ContextType.SKILL else c.uri
                    for c in grouped[:limit]
                }
            else:
                current_topk = sorted(
                    collected_by_uri.values(),
                    key=lambda x: x.get("_final_score", 0),
                    reverse=True,
                )[:limit]
                current_topk_uris = {c.get("uri", "") for c in current_topk}
                current_topk_state = current_topk_uris
            current_pool_size = len(collected_by_uri)

            if current_topk_state == prev_topk_state and len(current_topk_uris) >= limit:
                convergence_rounds += 1

                if convergence_rounds >= self.MAX_CONVERGENCE_ROUNDS:
                    break
            elif current_pool_size == prev_pool_size and current_topk_state == prev_topk_state:
                stagnant_rounds += 1

                if stagnant_rounds >= self.MAX_CONVERGENCE_ROUNDS and not (
                    skill_resolver and has_skill and len(current_topk_uris) < limit
                ):
                    break
            else:
                convergence_rounds = 0
                stagnant_rounds = 0
                prev_topk_state = current_topk_state
                prev_pool_size = current_pool_size

        collected = sorted(
            collected_by_uri.values(),
            key=lambda x: x.get("_final_score", 0),
            reverse=True,
        )
        return collected if skill_resolver and has_skill else collected[:limit]

    async def _convert_to_matched_contexts(
        self,
        candidates: List[Dict[str, Any]],
        ctx: RequestContext,
        apply_hotness: bool = True,
    ) -> List[MatchedContext]:
        """Convert candidate results to MatchedContext list.

        Blends semantic similarity with a hotness score derived from
        ``active_count`` and ``updated_at`` when configured. The blend weight
        is controlled by ``retrieval.hotness_alpha`` (0 disables the boost).
        """
        results = []
        for c in candidates:
            # Fix: clamp inf/nan scores from vector search (#inf-score)
            semantic_score = self._finite_score(c.get("_final_score", c.get("_score", 0.0)))

            alpha = self.hotness_alpha
            if apply_hotness and alpha > 0:
                updated_at_raw = c.get("updated_at")
                if isinstance(updated_at_raw, str):
                    try:
                        updated_at_val = parse_iso_datetime(updated_at_raw)
                    except (ValueError, TypeError):
                        updated_at_val = None
                elif isinstance(updated_at_raw, datetime):
                    updated_at_val = updated_at_raw
                else:
                    updated_at_val = None

                h_score = hotness_score(
                    active_count=c.get("active_count", 0),
                    updated_at=updated_at_val,
                )
                final_score = (1 - alpha) * semantic_score + alpha * h_score
            else:
                final_score = semantic_score
            if not math.isfinite(final_score):
                final_score = 0.0
            level = c.get("level", 2)
            display_uri = self._append_level_suffix(c.get("uri", ""), level)
            abstract = c.get("abstract", "")
            if level in {ContextLevel.ABSTRACT, ContextLevel.OVERVIEW}:
                # New records persist body-only rerank scalars, but imported or
                # legacy indexes may still contain the full OKF document. Keep
                # the public find/search preview contract body-only at its final
                # conversion boundary. L2 user Markdown is intentionally left
                # untouched, including ordinary YAML frontmatter.
                try:
                    abstract = body_for_preview(abstract)
                except AbstractOverviewFormatError as exc:
                    logger.warning("Malformed sidecar in retrieval result %s: %s", display_uri, exc)
                    abstract = ""

            results.append(
                MatchedContext(
                    uri=display_uri,
                    context_type=ContextType(c["context_type"])
                    if c.get("context_type")
                    else ContextType.RESOURCE,
                    level=level,
                    abstract=abstract,
                    category=c.get("category", ""),
                    score=final_score,
                    search_tags=normalize_search_tags(c.get("search_tags"), discard_invalid=True),
                )
            )

        # Re-sort by blended score so hotness boost can change ranking
        results.sort(key=lambda x: x.score, reverse=True)
        return results

    @classmethod
    def _append_level_suffix(cls, uri: str, level: int) -> str:
        """Return user-facing URI with L0/L1 suffix reconstructed by level."""
        suffix = cls.LEVEL_URI_SUFFIX.get(level)
        if not uri or not suffix:
            return uri
        if uri.endswith(f"/{suffix}"):
            return uri
        if uri.endswith("/.abstract.md") or uri.endswith("/.overview.md"):
            return uri
        if uri.endswith("/") and not uri.endswith("://"):
            uri = uri.rstrip("/")
        return f"{uri}/{suffix}"
