# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Small bounded-concurrency helpers for file-sized batch work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def bounded_map(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    concurrency: int,
) -> list[R]:
    """Apply ``worker`` with a fixed number of tasks and preserve input order.

    Unlike ``gather(*(worker(item) for item in items))``, only ``concurrency``
    tasks exist at once. A failed worker prevents new items from being claimed,
    then all active workers are joined before the original failure is re-raised.
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")

    iterator = iter(enumerate(items))
    cursor_lock = asyncio.Lock()
    stop = asyncio.Event()
    results: dict[int, R] = {}

    async def next_item() -> tuple[int, T] | None:
        async with cursor_lock:
            if stop.is_set():
                return None
            try:
                return next(iterator)
            except StopIteration:
                return None

    async def run_worker() -> None:
        while item := await next_item():
            index, value = item
            try:
                results[index] = await worker(value)
            except BaseException:
                stop.set()
                raise

    workers = [asyncio.create_task(run_worker()) for _ in range(concurrency)]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return [results[index] for index in range(len(results))]
