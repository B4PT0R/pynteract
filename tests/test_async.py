"""
Tests de non-régression pour la couche async :
  - Shell.arun()
  - Collector async (async with)
  - Stream : hooks async
"""
import asyncio
import sys
import pytest
from pynteract import Shell
from pynteract.collector import Collector
from pynteract.streams import Stream


# ── Shell.arun() ───────────────────────────────────────────────────────────────

class TestArun:
    async def test_simple_expression(self, shell):
        r = await shell.arun("1 + 1")
        assert r.result == 2

    async def test_statement_result_is_none(self, shell):
        r = await shell.arun("x = 99")
        assert r.result is None

    async def test_stdout_captured(self, shell):
        r = await shell.arun("print('async!')")
        assert "async!" in r.stdout

    async def test_stderr_captured(self, shell):
        r = await shell.arun("import sys; sys.stderr.write('err\\n')")
        assert "err" in r.stderr

    async def test_namespace_persists_across_arun(self, shell):
        await shell.arun("z = 7")
        r = await shell.arun("z * 6")
        assert r.result == 42

    async def test_namespace_shared_between_run_and_arun(self, shell):
        shell.run("val = 10")
        r = await shell.arun("val + 5")
        assert r.result == 15

    async def test_arun_after_run_sees_same_namespace(self, stateful_shell):
        r = await stateful_shell.arun("x")
        assert r.result == 42

    async def test_runtime_error_captured(self, shell):
        r = await shell.arun("1 / 0")
        assert r.exception is not None
        assert isinstance(r.exception, ZeroDivisionError)

    async def test_syntax_error_captured(self, shell):
        # SyntaxError est capturée par le handler de Shell et stockée dans r.exception.
        r = await shell.arun("def bad(:")
        assert r.exception is not None
        assert isinstance(r.exception, SyntaxError)

    async def test_error_does_not_poison_namespace(self, shell):
        await shell.arun("safe = 'alive'")
        await shell.arun("raise RuntimeError('boom')")
        r = await shell.arun("safe")
        assert r.result == "alive"

    async def test_concurrent_arun_different_shells(self):
        """Two independent shells can run concurrently without crosstalk."""
        s1, s2 = Shell(), Shell()
        await asyncio.gather(s1.arun("a = 1"), s2.arun("a = 2"))
        r1 = await s1.arun("a")
        r2 = await s2.arun("a")
        assert r1.result == 1
        assert r2.result == 2

    async def test_top_level_await_yields_to_caller_loop(self, shell):
        entered, release = asyncio.Event(), asyncio.Event()
        shell.update_namespace(entered=entered, release=release)
        task = asyncio.create_task(shell.arun("entered.set()\nawait release.wait()\n42"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not task.done()
        release.set()
        response = await task
        assert response.exception is None
        assert response.result == 42

    async def test_contextvars_propagated(self, shell):
        """Read the actual caller ContextVar from inside executed code."""
        import contextvars

        var = contextvars.ContextVar("caller_value", default="missing")
        token = var.set("propagated")
        shell.update_namespace(var=var)
        try:
            response = await shell.arun("var.get()")
            assert response.exception is None
            assert response.result == "propagated"
            assert var.get() == "propagated"
        finally:
            var.reset(token)


# ── Async stdout hook ──────────────────────────────────────────────────────────

class TestAsyncHooks:
    async def test_async_stdout_hook_called(self, shell):
        received = []

        async def async_hook(data, buf):
            received.append(data)

        shell.hooks["stdout_hook"] = async_hook
        await shell.arun("print('from async hook')")
        assert any("from async hook" in chunk for chunk in received)

    async def test_async_stderr_hook_called(self, shell):
        received = []

        async def async_hook(data, buf):
            received.append(data)

        shell.hooks["stderr_hook"] = async_hook
        await shell.arun("import sys; sys.stderr.write('async err\\n')")
        assert any("async err" in chunk for chunk in received)

    async def test_sync_hook_still_works_in_arun(self, shell):
        received = []
        shell.hooks["stdout_hook"] = lambda data, buf: received.append(data)
        await shell.arun("print('sync in async')")
        assert any("sync in async" in chunk for chunk in received)

    async def test_async_hook_receives_cumulative_buffer(self, shell):
        buffers = []

        async def hook(data, buf):
            buffers.append(buf)

        shell.hooks["stdout_hook"] = hook
        await shell.arun("print('line1')\nprint('line2')")
        assert len(buffers) >= 2
        # Buffer must grow monotonically
        assert all(
            len(buffers[i]) <= len(buffers[i + 1])
            for i in range(len(buffers) - 1)
        )


# ── Collector async ────────────────────────────────────────────────────────────

class TestAsyncCollector:
    async def test_async_with_captures_stdout(self, shell):
        async with Collector(shell) as c:
            print("hello collector")
        assert "hello collector" in c.get_stdout()

    async def test_async_with_captures_stderr(self, shell):
        async with Collector(shell) as c:
            sys.stderr.write("err line\n")
        assert "err line" in c.get_stderr()

    async def test_async_with_suppresses_exception(self, shell):
        async with Collector(shell) as c:
            raise ValueError("caught!")
        assert isinstance(c.exception, ValueError)

    async def test_async_with_restores_streams(self, shell):
        real_stdout = sys.stdout
        async with Collector(shell):
            pass
        assert sys.stdout is real_stdout

    async def test_async_with_nested(self, shell):
        """Nested async collectors each capture their own output."""
        async with Collector(shell) as outer:
            print("outer line")
            async with Collector(shell) as inner:
                print("inner line")
        assert "inner line" in inner.get_stdout()
        # outer also sees inner (it was the active stream when inner printed)
        assert "outer line" in outer.get_stdout()

    async def test_sync_and_async_collector_equivalent(self, shell):
        """sync `with` and `async with` must produce the same stdout."""
        with Collector(shell) as c_sync:
            print("same output")
        sync_out = c_sync.get_stdout()

        async with Collector(shell) as c_async:
            print("same output")
        async_out = c_async.get_stdout()

        assert sync_out == async_out
