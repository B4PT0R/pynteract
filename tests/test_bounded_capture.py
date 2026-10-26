"""Explicit capture limits bound both stored text and hook backlog."""
import asyncio
import tracemalloc

import pytest

from pynteract import Shell
from pynteract.collector import Collector
from pynteract.streams import TRUNCATION_MARKER


@pytest.mark.parametrize("value", [-1, 1.5, True, "100"])
def test_invalid_limit(value):
    with pytest.raises(ValueError, match="capture_limit"):
        Shell(capture_limit=value)


@pytest.mark.parametrize("limit", [0, 1, 4096])
def test_large_write_is_bounded_and_visible(limit):
    delivered = []
    with Shell(capture_limit=limit, hook_buffer=False,
               stdout_hook=lambda data, buffer: delivered.append((data, buffer))) as shell:
        response = shell.run("print('x' * 2_000_000)")
    assert response.output_stats["stdout"] == {"written_chars": 2_000_001, "truncated": True}
    assert response.stdout == "x" * limit + TRUNCATION_MARKER
    assert "".join(data for data, _ in delivered) == response.stdout
    assert all(buffer == "" for _, buffer in delivered)


@pytest.mark.asyncio
async def test_slow_hook_many_writes_bounds_backlog_and_memory():
    delivered = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(data, buffer):
        entered.set()
        await release.wait()
        delivered.append((data, buffer))

    with Shell(capture_limit=4096, hook_buffer=False, stdout_hook=hook) as shell:
        async with Collector(shell) as collector:
            stream = collector.stdout_stream
            stream.write("first\n")
            await entered.wait()
            tracemalloc.start()
            try:
                for _ in range(100_000):
                    stream.write("x\n")
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            assert peak < 4_000_000
            assert len(stream.async_hooks.pending) <= 4096
            release.set()
        assert collector.get_stdout().endswith(TRUNCATION_MARKER)
        assert len(collector.get_stdout()) == 4096 + len(TRUNCATION_MARKER)
    assert "".join(data for data, _ in delivered) == collector.get_stdout()
    assert all(buffer == "" for _, buffer in delivered)


def test_default_preserves_complete_capture_and_cumulative_hooks():
    delivered = []
    with Shell(stdout_hook=lambda data, buffer: delivered.append(buffer)) as shell:
        response = shell.run("print('a'); print('b')")
    assert response.stdout == "a\nb\n"
    assert delivered == ["a\n", "a\nb\n"]


def test_limit_is_per_stream_per_run_and_namespace_survives():
    with Shell(capture_limit=3, stdout_hook=lambda *_: None,
               stderr_hook=lambda *_: None) as shell:
        response = shell.run("import sys; value = 42; print('abcdef'); sys.stderr.write('uvwxyz'); None")
        assert response.stdout == "abc" + TRUNCATION_MARKER
        assert response.stderr == "uvw" + TRUNCATION_MARKER
        assert shell.run("value").stdout == "42\n"


@pytest.mark.asyncio
async def test_interrupt_after_capture_overflow_keeps_namespace():
    from pynteract import ExecutionInterrupted

    ready = asyncio.Event()
    with Shell(capture_limit=100, stdout_hook=lambda *_: None) as shell:
        shell.namespace.update(ready=ready, asyncio=asyncio)
        task = asyncio.create_task(shell.arun(
            "kept = 42; print('x' * 10000); ready.set(); await asyncio.sleep(30)"))
        await ready.wait()
        assert shell.interrupt()
        with pytest.raises(ExecutionInterrupted):
            await asyncio.wait_for(task, 2)
        assert (await shell.arun("kept")).stdout == "42\n"


@pytest.mark.parametrize("text,limit,truncated", [("", 0, False), ("abc", 3, False),
                                                   ("abcd", 3, True)])
def test_exact_boundary_and_stats(text, limit, truncated):
    with Shell(capture_limit=limit, display_mode="none", stdout_hook=lambda *_: None) as shell:
        shell.namespace["text"] = text
        response = shell.run("import sys; sys.stdout.write(text)")
    assert response.output_stats["stdout"] == {"written_chars": len(text), "truncated": truncated}
    assert response.stdout == text[:limit] + (TRUNCATION_MARKER if truncated else "")


@pytest.mark.asyncio
async def test_async_cumulative_snapshots_remain_emission_time_prefixes():
    delivered = []

    async def hook(data, buffer):
        await asyncio.sleep(0)
        delivered.append((data, buffer))

    with Shell(capture_limit=5, stdout_hook=hook) as shell:
        response = await shell.arun("print('a'); print('bc'); print('discarded')")
    assert delivered == [("a\n", "a\n"), ("bc\n", "a\nbc\n"),
                         (TRUNCATION_MARKER, "a\nbc\n" + TRUNCATION_MARKER)]
    assert response.output_stats["stdout"] == {"written_chars": 15, "truncated": True}
