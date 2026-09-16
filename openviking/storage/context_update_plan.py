# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Build and execute explicit content, semantic and index actions."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping

from openviking.storage.resource_diff import ContentState, IndexState
from openviking.storage.resource_rnfv import (
    NON_PORTABLE_VECTOR_RECORD_FIELDS,
    FormalTreeSnapshot,
    NewArtifactSnapshot,
    RequestIntent,
    RNFVSnapshot,
    VectorRecordSnapshot,
)
from openviking.storage.vector_ids import vector_record_id
from openviking.storage.viking_fs._diff_plan import NewEntry, TargetFile
from openviking.utils.ingest_options import IngestOptions
from openviking_cli.utils import VikingURI


class SemanticAction(str, Enum):
    REUSE = "reuse"
    GENERATE = "generate"
    AGGREGATE = "aggregate"


class IndexOperation(str, Enum):
    NONE = "none"
    UPSERT = "upsert"
    DELETE = "delete"
    UPDATE_FIELDS = "update_fields"


class SemanticOutputCondition(str, Enum):
    OUTPUT_READY = "output_ready"
    OUTPUT_CHANGED = "output_changed"


class FileVectorSource(str, Enum):
    CONTENT = "content"
    SUMMARY_WHEN_AVAILABLE = "summary_when_available"


@dataclass(frozen=True)
class ParentPropagation:
    enabled: bool = True


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("relative_path must be a string")
    if not value:
        return value
    path = PurePosixPath(value)
    if path.is_absolute() or value != str(path) or any(part == ".." for part in path.parts):
        raise ValueError(f"invalid relative path: {value}")
    return value


def _validate_index_fields(fields: Mapping[str, Any]) -> None:
    if set(fields) & {"id", "uri", "level", "vector", "sparse_vector"}:
        raise ValueError("index fields contain identity or vector payload")


@dataclass(frozen=True)
class IndexSlot:
    level: int
    record_id: str
    existing_fields: Mapping[str, Any] | None = None
    operation: IndexOperation = IndexOperation.NONE
    trigger: SemanticOutputCondition | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    fallback_update_fields: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", IndexOperation(self.operation))
        if self.trigger is not None:
            object.__setattr__(self, "trigger", SemanticOutputCondition(self.trigger))
        if self.level not in {0, 1, 2} or not self.record_id:
            raise ValueError("index slot requires a level and record ID")
        if self.operation is IndexOperation.DELETE:
            raise ValueError("delete belongs to direct index actions")
        if self.operation is IndexOperation.UPDATE_FIELDS:
            raise ValueError("semantic index slots only supports none or upsert")
        if self.operation is IndexOperation.NONE and self.trigger is not None:
            raise ValueError("index trigger requires an operation")
        if self.operation is not IndexOperation.NONE and self.trigger is None:
            raise ValueError("semantic index operation requires a trigger")
        _validate_index_fields(self.existing_fields or {})
        _validate_index_fields(self.fields)

    @property
    def abstract(self) -> str:
        return str((self.existing_fields or {}).get("abstract") or "")

    def scalar_override(self) -> dict[str, Any]:
        return {
            **{
                key: value
                for key, value in (self.existing_fields or {}).items()
                if key not in NON_PORTABLE_VECTOR_RECORD_FIELDS and not key.startswith("_")
            },
            **dict(self.fields),
            "_record_id": self.record_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IndexSlot":
        return cls(**dict(data))


@dataclass(frozen=True)
class SemanticTreeEntry:
    relative_path: str
    kind: str
    content_state: ContentState
    semantic_action: SemanticAction
    md5: str | None = None
    index_slots: tuple[IndexSlot, ...] = ()
    membership_changed: bool = False
    repair: bool = False

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        object.__setattr__(self, "content_state", ContentState(self.content_state))
        object.__setattr__(self, "semantic_action", SemanticAction(self.semantic_action))
        if self.kind not in {"file", "directory"}:
            raise ValueError("invalid semantic entry kind")
        if self.kind == "file" and self.semantic_action is SemanticAction.AGGREGATE:
            raise ValueError("file semantic action must be generate or reuse")
        if self.kind == "directory" and self.semantic_action is SemanticAction.GENERATE:
            raise ValueError("directory semantic action must be aggregate or reuse")
        if self.kind == "directory" and self.md5:
            raise ValueError("directory entry cannot carry md5")
        levels = [slot.level for slot in self.index_slots]
        if len(levels) != len(set(levels)):
            raise ValueError(f"duplicate index slot level for {self.relative_path}")
        if self.semantic_action is SemanticAction.REUSE:
            if any(slot.operation is not IndexOperation.NONE for slot in self.index_slots):
                raise ValueError("reuse semantic entry cannot contain index mutations")
            slot = self.slot(2 if self.kind == "file" else 0)
            if slot is None or not slot.abstract.strip():
                raise ValueError("reuse requires an existing abstract")

    def slot(self, level: int) -> IndexSlot | None:
        return next((slot for slot in self.index_slots if slot.level == level), None)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticTreeEntry":
        values = dict(data)
        values["index_slots"] = tuple(
            IndexSlot.from_dict(item) for item in values.get("index_slots", ())
        )
        return cls(**values)


@dataclass(frozen=True)
class SemanticTreeSnapshot:
    entries: tuple[SemanticTreeEntry, ...]

    def __post_init__(self) -> None:
        paths = [entry.relative_path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate semantic tree entry path")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticTreeSnapshot":
        return cls(
            entries=tuple(SemanticTreeEntry.from_dict(item) for item in data.get("entries", ()))
        )


@dataclass(frozen=True)
class SemanticPlan:
    root_uri: str
    context_type: str
    tree: SemanticTreeSnapshot
    vectorize: bool = True
    propagation: ParentPropagation = field(default_factory=ParentPropagation)
    file_vector_source: FileVectorSource = FileVectorSource.CONTENT
    ingest_options: IngestOptions = field(default_factory=IngestOptions)
    source_metadata: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_uri", VikingURI(self.root_uri).uri.rstrip("/"))
        object.__setattr__(self, "file_vector_source", FileVectorSource(self.file_vector_source))
        object.__setattr__(self, "ingest_options", IngestOptions.from_value(self.ingest_options))
        if self.context_type not in {"resource", "skill", "memory"}:
            raise ValueError(f"invalid context_type: {self.context_type}")
        if self.tree.entries and not any(entry.relative_path == "" for entry in self.tree.entries):
            raise ValueError("semantic plan must contain the resource root")
        paths = {entry.relative_path for entry in self.tree.entries}
        for entry in self.tree.entries:
            if not entry.relative_path:
                continue
            parent = _parent(entry.relative_path)
            if parent not in paths:
                raise ValueError(
                    f"semantic plan lacks parent {parent!r} for {entry.relative_path!r}"
                )
            ancestors = []
            current = parent
            while True:
                ancestors.append(current)
                if not current:
                    break
                current = _parent(current)
            for ancestor in ancestors:
                ancestor_entry = next(
                    item for item in self.tree.entries if item.relative_path == ancestor
                )
                if (
                    entry.semantic_action is not SemanticAction.REUSE
                    and ancestor_entry.semantic_action is not SemanticAction.AGGREGATE
                ):
                    raise ValueError(
                        f"semantic plan ancestor {ancestor!r} must aggregate active descendant "
                        f"{entry.relative_path!r}"
                    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["file_vector_source"] = self.file_vector_source.value
        data["ingest_options"] = self.ingest_options.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticPlan":
        return cls(
            root_uri=str(data["root_uri"]),
            context_type=str(data["context_type"]),
            tree=SemanticTreeSnapshot.from_dict(data.get("tree", {})),
            vectorize=bool(data.get("vectorize", True)),
            propagation=ParentPropagation(**dict(data.get("propagation", {}))),
            file_vector_source=FileVectorSource(
                data.get("file_vector_source", FileVectorSource.CONTENT.value)
            ),
            ingest_options=IngestOptions.from_value(data.get("ingest_options")),
            source_metadata=data.get("source_metadata"),
        )

    def execution_root_uris(self) -> tuple[str, ...]:
        directories = {
            entry.relative_path
            for entry in self.tree.entries
            if entry.kind == "directory" and entry.semantic_action is SemanticAction.AGGREGATE
        }
        roots = sorted(
            path
            for path in directories
            if not any(
                parent != path
                and (not parent or path.startswith(parent + "/"))
                and parent in directories
                for parent in directories
            )
        )
        if not roots and any(
            entry.semantic_action is not SemanticAction.REUSE for entry in self.tree.entries
        ):
            roots = [""]
        return tuple(self.root_uri if not path else f"{self.root_uri}/{path}" for path in roots)


class ContentTreeOperation(str, Enum):
    UPSERT = "upsert"
    DELETE = "delete"
    REPLACE_KIND = "replace_kind"


@dataclass(frozen=True)
class ContentTreeAction:
    operation: ContentTreeOperation
    relative_path: str
    old_kind: str | None = None
    new_kind: str | None = None
    artifact_path: str | None = None
    md5: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", ContentTreeOperation(self.operation))
        _validate_relative_path(self.relative_path)
        if self.artifact_path is not None:
            _validate_relative_path(self.artifact_path)
        if self.operation is not ContentTreeOperation.DELETE and self.new_kind == "file":
            if self.artifact_path is None or not self.md5:
                raise ValueError("file write requires artifact_path and md5")
        if self.operation is ContentTreeOperation.REPLACE_KIND and (
            not self.old_kind or not self.new_kind or self.old_kind == self.new_kind
        ):
            raise ValueError("replacement requires different old and new kinds")


@dataclass(frozen=True)
class IndexAction:
    operation: IndexOperation
    uri: str
    level: int
    record_id: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    md5: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", IndexOperation(self.operation))
        if self.operation is IndexOperation.NONE or self.level not in {0, 1, 2}:
            raise ValueError("invalid direct index action")
        if not self.record_id:
            raise ValueError("direct index action requires a record ID")
        _validate_index_fields(self.fields)
        if self.operation is IndexOperation.UPDATE_FIELDS and not self.fields:
            raise ValueError("field update requires fields")


@dataclass(frozen=True)
class ContextUpdatePlan:
    root_uri: str
    context_type: str
    content_tree_actions: tuple[ContentTreeAction, ...] = ()
    semantic_plan: SemanticPlan | None = None
    direct_index_actions: tuple[IndexAction, ...] = ()

    def __post_init__(self) -> None:
        root = self.root_uri.rstrip("/")
        paths = [action.relative_path for action in self.content_tree_actions]
        if len(paths) != len(set(paths)):
            raise ValueError("conflicting content actions")
        record_ids: set[str] = set()
        for action in self.direct_index_actions:
            if action.uri != root and not action.uri.startswith(root + "/"):
                raise ValueError("index action outside root")
            if action.record_id in record_ids:
                raise ValueError("conflicting index actions")
            record_ids.add(action.record_id)
        if self.semantic_plan is not None:
            if (
                self.semantic_plan.root_uri != root
                or self.semantic_plan.context_type != self.context_type
            ):
                raise ValueError("semantic plan identity mismatch")
            for entry in self.semantic_plan.tree.entries:
                for slot in entry.index_slots:
                    if slot.operation is IndexOperation.NONE:
                        continue
                    if slot.record_id in record_ids:
                        raise ValueError("conflicting index actions")
                    record_ids.add(slot.record_id)

    def is_noop(self) -> bool:
        return (
            not self.content_tree_actions
            and self.semantic_plan is None
            and not self.direct_index_actions
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.semantic_plan is not None:
            data["semantic_plan"] = self.semantic_plan.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextUpdatePlan":
        return cls(
            root_uri=str(data["root_uri"]),
            context_type=str(data["context_type"]),
            content_tree_actions=tuple(
                ContentTreeAction(**item) for item in data.get("content_tree_actions", ())
            ),
            semantic_plan=(
                SemanticPlan.from_dict(data["semantic_plan"]) if data.get("semantic_plan") else None
            ),
            direct_index_actions=tuple(
                IndexAction(**item) for item in data.get("direct_index_actions", ())
            ),
        )


def _uri(root_uri: str, relative_path: str) -> str:
    return root_uri if not relative_path else f"{root_uri}/{relative_path}"


def _parent(relative_path: str) -> str:
    value = str(PurePosixPath(relative_path).parent)
    return "" if value == "." else value


def _records_by_path(
    records: Mapping[str, VectorRecordSnapshot],
) -> tuple[
    dict[str, dict[int, VectorRecordSnapshot]],
    tuple[VectorRecordSnapshot, ...],
]:
    result: dict[str, dict[int, VectorRecordSnapshot]] = {}
    duplicates: list[VectorRecordSnapshot] = []
    for record in records.values():
        levels = result.setdefault(record.relative_path, {})
        current = levels.get(record.level)
        if current is None:
            levels[record.level] = record
        elif record.record_id < current.record_id:
            duplicates.append(current)
            levels[record.level] = record
        else:
            duplicates.append(record)
    return result, tuple(duplicates)


def _portable_existing_fields(record: VectorRecordSnapshot | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        key: value
        for key, value in record.fields.items()
        if key not in {"id", "uri", "level", "vector", "sparse_vector", "content"}
        and value is not None
    }


def _resolved_scalar_fields(
    request: RequestIntent, record: VectorRecordSnapshot | None
) -> dict[str, Any]:
    from openviking.utils.tags import merge_search_tags, normalize_search_tags

    existing = dict(record.fields) if record is not None else {}
    result: dict[str, Any] = {}
    for intent in request.scalar_intents:
        if record is not None and record.level not in intent.target_levels:
            continue
        if intent.field != "search_tags":
            continue
        old = normalize_search_tags(existing.get(intent.field), discard_invalid=True)
        incoming = normalize_search_tags(intent.value, discard_invalid=True)
        desired = merge_search_tags(old, incoming) if intent.mode == "append" else incoming
        if sorted(old) != sorted(desired):
            result[intent.field] = desired
    return result


def _semantic_closure(
    diff: Any,
    new_kinds: Mapping[str, str],
    *,
    repair_indexes: bool = True,
) -> tuple[set[str], set[str], set[str]]:
    active: set[str] = set()
    membership_changed: set[str] = set()

    def add_ancestors(path: str) -> None:
        parent = _parent(path)
        while True:
            active.add(parent)
            if not parent:
                return
            parent = _parent(parent)

    for path, entry in diff.entries.items():
        content_state = ContentState(entry.content_state)
        index_state = IndexState(entry.index_state)
        content_requires_semantics = content_state in {
            ContentState.ADDED,
            ContentState.MODIFIED,
            ContentState.REPLACE_KIND,
        } or (content_state is ContentState.RESTORE and index_state is not IndexState.COMPLETE)
        if (
            content_requires_semantics
            or (
                repair_indexes
                and index_state
                in {IndexState.MISSING, IndexState.PARTIAL, IndexState.LEVEL_CONFLICT}
            )
        ) and entry.new_kind in {"file", "directory"}:
            active.add(path)
            add_ancestors(path)
        if content_state in {
            ContentState.ADDED,
            ContentState.DELETED,
            ContentState.RESTORE,
            ContentState.REPLACE_KIND,
        }:
            membership_changed.add(_parent(path))
            add_ancestors(path)

    retained = set(active)
    for path in new_kinds:
        if path and _parent(path) in active:
            retained.add(path)
    return active, membership_changed, retained


async def hydrate_context_plan_records(
    *,
    diff: Any,
    new_kinds: Mapping[str, str],
    inventory: Mapping[str, VectorRecordSnapshot],
    vikingdb: Any,
    ctx: Any,
    repair_indexes: bool = True,
) -> tuple[Mapping[str, VectorRecordSnapshot], tuple[set[str], set[str], set[str]]]:
    active, membership_changed, retained = _semantic_closure(
        diff, new_kinds, repair_indexes=repair_indexes
    )
    result = dict(inventory)
    hydrated_ids: set[str] = set()
    while True:
        required: dict[str, Mapping[str, Any]] = {}
        for record_id, record in inventory.items():
            if record.relative_path not in retained or record_id in hydrated_ids:
                continue
            kind = new_kinds.get(record.relative_path)
            wanted = {2} if kind == "file" else {0, 1} if record.relative_path in active else {0}
            if record.level in wanted:
                required[record_id] = {"uri": record.uri, "level": record.level}
        hydrated = await vikingdb.hydrate_incremental_records(required, ctx=ctx) if required else {}
        # Inventory and hydration are separate reads. A missing hydration result
        # has no reusable abstract, so the dependency is promoted below. Keep
        # the inventory identity: an existing record must never be replaced by a
        # locally recomputed ID merely because its second read raced or failed.
        for record_id, payload in hydrated.items():
            current = inventory[record_id]
            fields = {
                key: value
                for key, value in payload.items()
                if key not in {"id", "uri", "level", "vector", "sparse_vector", "content"}
                and value is not None
            }
            result[record_id] = VectorRecordSnapshot(
                current.record_id, current.uri, current.relative_path, current.level, fields
            )
        hydrated_ids.update(required)

        by_path, _ = _records_by_path(result)
        promoted = {
            path
            for path in retained - active
            if (
                (record := by_path.get(path, {}).get(2 if new_kinds.get(path) == "file" else 0))
                is None
                or not str(record.fields.get("abstract") or "").strip()
            )
        }
        if not promoted:
            return result, (active, membership_changed, retained)
        active.update(promoted)
        for path in new_kinds:
            if path and _parent(path) in active:
                retained.add(path)


def build_context_update_plan(
    *,
    root_uri: str,
    context_type: str,
    request: RequestIntent,
    diff: Any,
    new_kinds: Mapping[str, str],
    artifact_paths: Mapping[str, str],
    records: Mapping[str, VectorRecordSnapshot],
    is_code_repo: bool,
    account_id: str,
    ingest_options: IngestOptions | Mapping[str, Any] | None = None,
    source_metadata: Mapping[str, str] | None = None,
    closure: tuple[set[str], set[str], set[str]] | None = None,
) -> ContextUpdatePlan:
    root_uri = root_uri.rstrip("/")
    records_by_path, duplicate_records = _records_by_path(records)
    active, membership_changed, retained = closure or _semantic_closure(
        diff,
        new_kinds,
        repair_indexes=request.processing_mode != "vectors_only",
    )
    semantic_enabled = request.processing_mode != "vectors_only"
    if not semantic_enabled:
        active, membership_changed, retained = set(), set(), set()

    content_actions: list[ContentTreeAction] = []
    direct_actions: list[IndexAction] = [
        IndexAction(IndexOperation.DELETE, record.uri, record.level, record.record_id)
        for record in duplicate_records
    ]
    for path, entry in sorted(diff.entries.items()):
        state = ContentState(entry.content_state)
        kind = entry.new_kind
        if state in {ContentState.ADDED, ContentState.RESTORE, ContentState.MODIFIED}:
            content_actions.append(
                ContentTreeAction(
                    ContentTreeOperation.UPSERT,
                    path,
                    entry.old_kind,
                    kind,
                    artifact_paths.get(path),
                    entry.md5,
                )
            )
        elif state is ContentState.DELETED:
            content_actions.append(
                ContentTreeAction(ContentTreeOperation.DELETE, path, old_kind=entry.old_kind)
            )
        elif state is ContentState.REPLACE_KIND:
            content_actions.append(
                ContentTreeAction(
                    ContentTreeOperation.REPLACE_KIND,
                    path,
                    entry.old_kind,
                    kind,
                    artifact_paths.get(path),
                    entry.md5,
                )
            )

        existing = records_by_path.get(path, {})
        valid_levels = {2} if kind == "file" else {0, 1} if kind == "directory" else set()
        for level, record in existing.items():
            if state in {ContentState.DELETED, ContentState.ABSENT} or level not in valid_levels:
                direct_actions.append(
                    IndexAction(IndexOperation.DELETE, record.uri, level, record.record_id)
                )
        if (
            not semantic_enabled
            and request.vectorize
            and kind == "file"
            and (
                state
                in {
                    ContentState.ADDED,
                    ContentState.MODIFIED,
                    ContentState.REPLACE_KIND,
                }
                or existing.get(2) is None
                or IndexState(entry.index_state) is IndexState.STALE
            )
        ):
            record = existing.get(2)
            direct_actions.append(
                IndexAction(
                    IndexOperation.UPSERT,
                    _uri(root_uri, path),
                    2,
                    record.record_id
                    if record is not None
                    else vector_record_id(account_id, _uri(root_uri, path), 2),
                    fields={
                        **(_portable_existing_fields(record) or {}),
                        **_resolved_scalar_fields(request, record),
                    },
                    md5=entry.md5,
                )
            )

    scheduled_direct_ids = {action.record_id for action in direct_actions}
    for path, levels in records_by_path.items():
        for record in levels.values():
            fields = _resolved_scalar_fields(request, record)
            diff_entry = diff.entries.get(path)
            if (
                record.level == 2
                and diff_entry is not None
                and ContentState(diff_entry.content_state) is ContentState.UNCHANGED
                and diff_entry.md5
                and not str(record.fields.get("md5") or "")
            ):
                fields["md5"] = diff_entry.md5
            if (
                fields
                and record.record_id not in scheduled_direct_ids
                and (path not in active or not request.vectorize)
            ):
                direct_actions.append(
                    IndexAction(
                        IndexOperation.UPDATE_FIELDS,
                        record.uri,
                        record.level,
                        record.record_id,
                        fields=fields,
                    )
                )
                scheduled_direct_ids.add(record.record_id)

    semantic_entries: list[SemanticTreeEntry] = []
    for path in sorted(retained, key=lambda value: (value.count("/"), value)):
        kind = new_kinds.get(path)
        if kind not in {"file", "directory"}:
            continue
        diff_entry = diff.entries.get(path)
        state = ContentState(diff_entry.content_state) if diff_entry else ContentState.UNCHANGED
        action = (
            SemanticAction.GENERATE
            if kind == "file" and path in active
            else SemanticAction.AGGREGATE
            if kind == "directory" and path in active
            else SemanticAction.REUSE
        )
        slots: list[IndexSlot] = []
        for level in (2,) if kind == "file" else (0, 1):
            record = records_by_path.get(path, {}).get(level)
            if action is SemanticAction.REUSE:
                if level != (2 if kind == "file" else 0):
                    continue
                if record is None or not str(record.fields.get("abstract") or "").strip():
                    raise ValueError(f"semantic closure lacks reusable abstract for {path}")
                slots.append(IndexSlot(level, record.record_id, _portable_existing_fields(record)))
                continue
            record_id = (
                record.record_id
                if record is not None
                else vector_record_id(account_id, _uri(root_uri, path), level)
            )
            fields = _resolved_scalar_fields(request, record)
            slots.append(
                IndexSlot(
                    level,
                    record_id,
                    _portable_existing_fields(record),
                    operation=IndexOperation.UPSERT if request.vectorize else IndexOperation.NONE,
                    trigger=(
                        SemanticOutputCondition.OUTPUT_READY
                        if record is None
                        or state
                        in {ContentState.ADDED, ContentState.RESTORE, ContentState.REPLACE_KIND}
                        else SemanticOutputCondition.OUTPUT_CHANGED
                    )
                    if request.vectorize
                    else None,
                    fields=fields,
                    fallback_update_fields=(state is ContentState.MODIFIED or bool(fields)),
                )
            )
        semantic_entries.append(
            SemanticTreeEntry(
                path,
                kind,
                state,
                action,
                md5=diff_entry.md5 if diff_entry else None,
                index_slots=tuple(slots),
                membership_changed=path in membership_changed,
                repair=bool(
                    diff_entry
                    and IndexState(diff_entry.index_state)
                    in {IndexState.MISSING, IndexState.PARTIAL, IndexState.LEVEL_CONFLICT}
                ),
            )
        )

    semantic_plan = (
        SemanticPlan(
            root_uri,
            context_type,
            SemanticTreeSnapshot(tuple(semantic_entries)),
            vectorize=request.vectorize,
            file_vector_source=(
                FileVectorSource.SUMMARY_WHEN_AVAILABLE
                if is_code_repo
                else FileVectorSource.CONTENT
            ),
            ingest_options=IngestOptions.from_value(ingest_options),
            source_metadata=source_metadata,
        )
        if semantic_entries
        else None
    )
    return ContextUpdatePlan(
        root_uri,
        context_type,
        tuple(content_actions),
        semantic_plan,
        tuple(direct_actions),
    )


def _with_directory_root(snapshot: RNFVSnapshot, *, root_preexisting: bool) -> RNFVSnapshot:
    """Add the logical directory root omitted by artifact/tree walks."""
    new_entries = {"": NewEntry(is_dir=True), **snapshot.new.entries}
    formal_entries = (
        {"": TargetFile(is_dir=True), **snapshot.formal.entries}
        if root_preexisting
        else dict(snapshot.formal.entries)
    )
    return RNFVSnapshot(
        request=snapshot.request,
        new=NewArtifactSnapshot(new_entries, complete=snapshot.new.complete),
        formal=FormalTreeSnapshot(formal_entries, complete=snapshot.formal.complete),
        vectors=snapshot.vectors,
    )


async def build_context_update_plan_from_snapshot(
    *,
    snapshot: RNFVSnapshot,
    store: Any,
    artifact_ref: Any,
    target: Any,
    vikingdb: Any,
    context_type: str,
    is_code_repo: bool,
    account_id: str,
    ctx: Any,
    root_preexisting: bool,
    artifact_paths: Mapping[str, str] | None = None,
    ingest_options: Any = None,
    source_metadata: Mapping[str, str] | None = None,
) -> tuple[Any, ContextUpdatePlan]:
    """Resolve RNFV facts, hydrate the minimal closure, and build one plan."""
    from openviking.storage.resource_diff import resolve_resource_diff

    snapshot = _with_directory_root(snapshot, root_preexisting=root_preexisting)
    paths = dict(artifact_paths or {})
    diff = await resolve_resource_diff(
        snapshot,
        store=store,
        artifact_ref=artifact_ref,
        target=target,
        artifact_paths=paths,
    )
    new_kinds = {
        path: "directory" if entry.is_dir else "file"
        for path, entry in snapshot.new.entries.items()
    }
    records, closure = await hydrate_context_plan_records(
        diff=diff,
        new_kinds=new_kinds,
        inventory=snapshot.vectors.records_by_id,
        vikingdb=vikingdb,
        ctx=ctx,
        repair_indexes=snapshot.request.processing_mode != "vectors_only",
    )
    return diff, build_context_update_plan(
        root_uri=snapshot.request.target_uri,
        context_type=context_type,
        request=snapshot.request,
        diff=diff,
        new_kinds=new_kinds,
        artifact_paths=paths,
        records=records,
        is_code_repo=is_code_repo,
        account_id=account_id,
        ingest_options=ingest_options,
        source_metadata=source_metadata,
        closure=closure,
    )


async def execute_content_tree_actions(
    actions: tuple[ContentTreeAction, ...],
    *,
    store: Any,
    artifact_ref: Any,
    target: Any,
    concurrency: int | None = None,
) -> None:
    """Commit planned content mutations before any asynchronous work."""
    destructive = [
        action
        for action in actions
        if action.operation in {ContentTreeOperation.DELETE, ContentTreeOperation.REPLACE_KIND}
    ]
    for action in sorted(
        destructive, key=lambda item: (-item.relative_path.count("/"), item.relative_path)
    ):
        await target.delete_file(action.relative_path)
    for action in sorted(
        (action for action in actions if action.new_kind == "directory"),
        key=lambda item: (item.relative_path.count("/"), item.relative_path),
    ):
        await target.mkdir(action.relative_path)
    file_actions = [action for action in actions if action.new_kind == "file"]
    if not file_actions:
        return
    if concurrency is None:
        from openviking.parse.parsers import upload_utils

        concurrency = int(getattr(upload_utils, "_UPLOAD_CONCURRENCY", 8))
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def write(action: ContentTreeAction) -> None:
        async with semaphore:
            data = await store.read_bytes(artifact_ref, action.artifact_path)
            await target.write_file(action.relative_path, data)

    await asyncio.gather(*(write(action) for action in file_actions))


__all__ = [
    "ContentState",
    "ContentTreeAction",
    "ContentTreeOperation",
    "ContextUpdatePlan",
    "FileVectorSource",
    "IndexAction",
    "IndexOperation",
    "IndexSlot",
    "IndexState",
    "ParentPropagation",
    "SemanticAction",
    "SemanticOutputCondition",
    "SemanticPlan",
    "SemanticTreeEntry",
    "SemanticTreeSnapshot",
    "build_context_update_plan",
    "build_context_update_plan_from_snapshot",
    "execute_content_tree_actions",
    "hydrate_context_plan_records",
]
