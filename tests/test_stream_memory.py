"""Queued hooks retain chunks and offsets, never cumulative text snapshots."""
import asyncio
import tracemalloc

import pytest

from pynteract.collector import Collector
from pynteract import Shell


@pytest.mark.asyncio
async def test_slow_hook_backlog_has_linear_memory_and_exact_snapshots():
    entered, release = asyncio.Event(), asyncio.Event()
    snapshots = []

    async def hook(data, buffer):
        if not snapshots:
            entered.set()
            await release.wait()
        # Retain just lengths: consumers can of course choose to keep snapshots.
        snapshots.append((len(data), len(buffer)))

    shell = Shell(stdout_hook=hook)
    async with Collector(shell) as collector:
        collector.stdout_stream.write('x' * 999 + '\n')
        await entered.wait()
        tracemalloc.start()
        try:
            for _ in range(1000):
                collector.stdout_stream.write('x' * 999 + '\n')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # 1 MB of actual text. Old eager snapshots retained >500 MB here.
        assert peak < 12_000_000
        release.set()
    assert snapshots == [(1000, i * 1000) for i in range(1, 1002)]
    assert len(collector.get_stdout()) == 1_001_000
