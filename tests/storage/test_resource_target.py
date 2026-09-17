# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for the AGFS resource target used by content tree actions."""

import pytest

from openviking.storage.resource_target import AgfsResourceTarget


class _Ctx:
    account_id = "acct"


class _FakeVikingFS:
    def __init__(self, existing=None):
        self.files = dict(existing or {})
        self.written = []
        self.removed = []

    async def write_file_bytes(self, uri, content, *, ctx=None, lease_ref=None):
        self.files[uri] = content
        self.written.append(uri)

    async def read_file_bytes(self, uri, *, ctx=None):
        return self.files[uri]

    async def rm(self, uri, *, recursive=False, ctx=None, lease_ref=None):
        self.removed.append(uri)
        self.files.pop(uri, None)

    async def remove_files(self, uri, *, recursive=False, ctx=None, lease_ref=None):
        self.removed.append(uri)
        self.files.pop(uri, None)


class _FakeVikingDB:
    def __init__(self):
        self.deleted_uris = []

    async def delete_uris(self, ctx, uris):
        self.deleted_uris.extend(uris)


_ROOT = "viking://resources/proj"


def _target(vfs, vikingdb):
    return AgfsResourceTarget(viking_fs=vfs, vikingdb=vikingdb, root_uri=_ROOT, ctx=_Ctx())


@pytest.mark.asyncio
class TestAgfsResourceTarget:
    async def test_write_joins_rel_path_under_root(self) -> None:
        vfs = _FakeVikingFS()
        target = _target(vfs, _FakeVikingDB())

        await target.write_file("sub/a.py", b"print(1)")

        assert vfs.files[f"{_ROOT}/sub/a.py"] == b"print(1)"

    async def test_empty_rel_path_targets_root_file(self) -> None:
        vfs = _FakeVikingFS()
        vikingdb = _FakeVikingDB()
        target = _target(vfs, vikingdb)

        await target.write_file("", b"body")
        await target.delete_vector("")

        assert vfs.files[_ROOT] == b"body"
        assert vikingdb.deleted_uris == [_ROOT]

    async def test_write_preserves_artifact_bytes(self) -> None:
        vfs = _FakeVikingFS()
        target = _target(vfs, _FakeVikingDB())

        artifact_bytes = "你好".encode("gbk")
        await target.write_file("a.py", artifact_bytes)

        assert vfs.files[f"{_ROOT}/a.py"] == artifact_bytes

    async def test_write_rejects_path_escape(self) -> None:
        vfs = _FakeVikingFS()
        target = _target(vfs, _FakeVikingDB())

        with pytest.raises(ValueError):
            await target.write_file("../evil.py", b"x")

    async def test_delete_file_removes_uri(self) -> None:
        vfs = _FakeVikingFS(existing={f"{_ROOT}/gone.py": b"x"})
        target = _target(vfs, _FakeVikingDB())

        await target.delete_file("gone.py")

        assert f"{_ROOT}/gone.py" in vfs.removed
        assert target._vikingdb.deleted_uris == []

    async def test_delete_vector_removes_l2_record(self) -> None:
        vikingdb = _FakeVikingDB()
        target = _target(_FakeVikingFS(), vikingdb)

        await target.delete_vector("ghost.py")

        assert vikingdb.deleted_uris == [f"{_ROOT}/ghost.py"]

    async def test_read_file_returns_existing_bytes(self) -> None:
        vfs = _FakeVikingFS(existing={f"{_ROOT}/a.py": b"body"})
        target = _target(vfs, _FakeVikingDB())

        assert await target.read_file("a.py") == b"body"
