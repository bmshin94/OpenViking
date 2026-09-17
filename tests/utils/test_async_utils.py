# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_bounded_map_only_claims_work_for_fixed_worker_count():
    import openviking.utils.async_utils as async_utils

    started = 0
    release = asyncio.Event()

    async def work(value: int) -> int:
        nonlocal started
        started += 1
        if started == 3:
            release.set()
        await release.wait()
        return value * 2

    create_task = asyncio.create_task
    with patch.object(async_utils.asyncio, "create_task", wraps=create_task) as created:
        result = await async_utils.bounded_map(range(100), work, concurrency=3)

    assert started == 100
    assert created.call_count == 3
    assert result == [index * 2 for index in range(100)]


@pytest.mark.asyncio
async def test_bounded_map_stops_claiming_after_worker_failure():
    from openviking.utils.async_utils import bounded_map

    started: list[int] = []
    release = asyncio.Event()

    async def work(value: int) -> int:
        started.append(value)
        if value == 0:
            raise RuntimeError("boom")
        await release.wait()
        return value

    task = asyncio.create_task(bounded_map(range(100), work, concurrency=3))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError, match="boom"):
        await task

    assert set(started) <= {0, 1, 2}
