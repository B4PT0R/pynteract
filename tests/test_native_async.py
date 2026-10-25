"""Native-loop execution, ownership, cancellation and task-local IO contracts."""
import asyncio
import contextlib
import contextvars
import io
import os
import sys
import threading

import pytest

from pynteract import Shell, get_run_ctx
from pynteract.context import CAPTURE_STDIN, CAPTURE_STREAMS


def quiet_shell(**kwargs):
    return Shell(display_mode="none", stdout_hook=lambda *args: None,
                 stderr_hook=lambda *args: None, **kwargs)


async def test_await_returns_original_object_on_callers_task():
    value = object()
    caller = asyncio.current_task()
    thread = threading.get_ident()
    shell = quiet_shell()

    async def get_value():
        assert asyncio.current_task() is caller
        assert threading.get_ident() == thread
        assert get_run_ctx().name == "native-cell"
        await asyncio.sleep(0)
        return value

    shell.update_namespace(get_value=get_value)
    response = await shell.arun("await get_value()", filename="native-cell")
    assert response.exception is None
    assert response.result is value
    assert shell.namespace["_"] is value
    assert shell.history["native-cell"] is response
    assert get_run_ctx() is None


async def test_plain_coroutine_expression_is_not_implicitly_awaited():
    shell = quiet_shell()

    async def value():
        return 42

    shell.update_namespace(value=value)
    response = await shell.arun("value()")
    assert asyncio.iscoroutine(response.result)
    assert await response.result == 42


async def test_async_statements_and_future_flags():
    shell = quiet_shell()
    response = await shell.arun("""
from __future__ import annotations
import asyncio
async def values():
    for value in range(3):
        await asyncio.sleep(0)
        yield value
result = []
async with asyncio.Lock():
    async for value in values():
        result.append(value)
def annotated(arg: NotDefined): pass
result
""")
    assert response.exception is None
    assert response.result == [0, 1, 2]
    assert shell.namespace["annotated"].__annotations__ == {"arg": "NotDefined"}
    assert shell.run("def later(arg: AlsoNotDefined): pass").exception is None


@pytest.mark.parametrize("mode, expected", [("none", []), ("last", []), ("all", [1, 2])])
async def test_display_semicolon_and_node_hook_order(mode, expected):
    displayed, events = [], []
    shell = quiet_shell()
    shell.display_mode = mode
    shell.hooks.update(
        display_hook=lambda result, kwargs: displayed.append(result),
        pre_execute_hook=lambda node, source: events.append("pre") or node,
        post_execute_hook=lambda node, result: events.append("post"),
    )
    shell.update_namespace(asyncio=asyncio)
    response = await shell.arun("await asyncio.sleep(0, result=1)\n2\n3;")
    assert response.exception is None
    assert displayed == expected
    assert response.result == 3
    assert events == ["pre", "post"] * 3


async def test_separate_shells_route_interleaved_stdio_and_leave_host_output_alone():
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def execute(name):
        shell = quiet_shell()
        shell.hooks["stdout_hook"] = lambda data, buf: seen.append((get_run_ctx().name, data))
        shell.hooks["stdin_hook"] = lambda: name
        shell.update_namespace(entered=entered, release=release)
        return await shell.arun(
            "import sys\nprint(input())\nsys.stderr.write('err\\n')\n"
            "entered.set()\nawait release.wait()\nprint(input())",
            filename=name,
        )

    saved = sys.stdout, sys.stderr, sys.stdin
    host = io.StringIO()
    with contextlib.redirect_stdout(host):
        first = asyncio.create_task(execute("first"))
        await entered.wait()
        entered.clear()
        second = asyncio.create_task(execute("second"))
        await entered.wait()
        print("host")
        release.set()
        # Start order is deliberately not finish order.
        r1, r2 = await asyncio.gather(first, second)
    assert (sys.stdout, sys.stderr, sys.stdin) == saved
    assert host.getvalue() == "host\n"
    assert r1.stdout == "first\nfirst\n"
    assert r2.stdout == "second\nsecond\n"
    assert r1.stderr == r2.stderr == "err\n"
    assert seen == [("first", "first\n"), ("second", "second\n"),
                    ("first", "first\n"), ("second", "second\n")]


async def test_same_shell_concurrency_rejected_without_poisoning_owner():
    entered, release = asyncio.Event(), asyncio.Event()
    shell = quiet_shell()
    shell.update_namespace(entered=entered, release=release)
    first = asyncio.create_task(shell.arun("entered.set()\nawait release.wait()\n7"))
    await entered.wait()
    with pytest.raises(RuntimeError, match="already executing"):
        await shell.arun("42")
    with pytest.raises(RuntimeError, match="already executing"):
        shell.run("42")
    release.set()
    assert (await first).result == 7
    assert shell.run("42").result == 42


@pytest.mark.parametrize("first_internal, second_internal", [(True, True), (True, False), (False, True)])
async def test_cwd_isolation_rejects_overlapping_cells(first_internal, second_internal):
    entered, release = asyncio.Event(), asyncio.Event()
    first = quiet_shell(use_internal_cwd=first_internal)
    second = quiet_shell(use_internal_cwd=second_internal)
    first.update_namespace(entered=entered, release=release)
    task = asyncio.create_task(first.arun("entered.set()\nawait release.wait()"))
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="Concurrent execution requires"):
            await second.arun("pass")
    finally:
        release.set()
        await task
    assert second.run("42").result == 42


async def test_cancellation_propagates_and_restores_everything(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    saved = sys.stdout, sys.stderr, sys.stdin
    previous_cwd = os.getcwd()
    shell = quiet_shell(use_internal_cwd=True)
    shell.update_namespace(entered=entered, release=release)
    task = asyncio.create_task(shell.arun("entered.set()\nawait release.wait()", cwd=tmp_path))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (sys.stdout, sys.stderr, sys.stdin) == saved
    assert os.getcwd() == previous_cwd
    assert get_run_ctx() is None
    assert CAPTURE_STREAMS.get() is None
    assert CAPTURE_STDIN.get() is None
    assert shell.run("42").result == 42


async def test_async_cell_error_response_and_enriched_traceback():
    shell = quiet_shell()
    response = await shell.arun("import asyncio\nawait asyncio.sleep(0)\n1 / 0", filename="awaited-cell")
    assert isinstance(response.exception, ZeroDivisionError)
    assert "awaited-cell" in response.exception.enriched_traceback_string
    assert "1 / 0" in response.stderr
    assert shell.run("42").result == 42


async def test_async_hooks_drained_in_order_with_original_context_and_no_recursion():
    finished = []
    host = io.StringIO()
    shell = quiet_shell()

    async def hook(data, buffer):
        await asyncio.sleep(0)
        finished.append((get_run_ctx().name, data, buffer))
        print("host hook")

    shell.hooks["stdout_hook"] = hook
    with contextlib.redirect_stdout(host):
        response = await shell.arun("print('a')\nprint('b', end='')", filename="hook-cell")
    assert response.stdout == "a\nb"
    assert finished == [("hook-cell", "a\n", "a\n"), ("hook-cell", "b", "a\nb")]
    assert host.getvalue() == "host hook\nhost hook\n"


async def test_async_hook_failure_is_observed_and_other_chunks_drained():
    seen = []
    shell = quiet_shell()
    saved = sys.stdout, sys.stderr, sys.stdin

    async def hook(data, buffer):
        await asyncio.sleep(0)
        seen.append(data)
        if data == "a\n":
            raise ValueError("delivery failed")

    shell.hooks["stdout_hook"] = hook
    with pytest.raises(ValueError, match="delivery failed"):
        await shell.arun("print('a')\nprint('b')")
    assert seen == ["a\n", "b\n"]
    assert (sys.stdout, sys.stderr, sys.stdin) == saved
    assert get_run_ctx() is None
    assert shell.run("42").result == 42


async def test_cancelling_hook_drain_cancels_owned_worker():
    entered, finished = asyncio.Event(), asyncio.Event()
    shell = quiet_shell()

    async def hook(data, buffer):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    shell.hooks["stdout_hook"] = hook
    task = asyncio.create_task(shell.arun("print('a')"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert shell.run("42").result == 42


def test_sync_run_rejects_async_stream_hooks_without_coroutine_leak():
    shell = quiet_shell()

    async def hook(data, buffer):
        pass

    shell.hooks["stdout_hook"] = hook
    response = shell.run("print('a')")
    assert isinstance(response.exception, TypeError)
    assert "require Shell.arun" in str(response.exception)


async def test_contextvars_changes_stay_on_callers_task():
    var = contextvars.ContextVar("native-change", default=0)
    shell = quiet_shell()
    shell.update_namespace(var=var)
    token = var.set(1)
    try:
        response = await shell.arun("var.set(2)\nvar.get()")
        assert response.result == var.get() == 2
    finally:
        var.reset(token)


@pytest.mark.parametrize("key", ["input_hook", "pre_run_hook", "post_run_hook"])
async def test_async_host_hook_is_rejected_without_leaking_coroutine(key):
    shell = quiet_shell()

    async def hook(*args):
        pass

    shell.hooks[key] = hook
    with pytest.raises(TypeError, match="must be synchronous"):
        await shell.arun("42")
    assert get_run_ctx() is None
    shell.hooks.pop(key)
    assert (await shell.arun("42")).result == 42


@pytest.mark.parametrize("key, code", [
    ("code_block_hook", "42"),
    ("pre_execute_hook", "42"),
    ("post_execute_hook", "42"),
    ("display_hook", "42"),
    ("stdin_hook", "input()"),
])
async def test_async_in_execution_hook_becomes_cell_type_error(key, code):
    shell = quiet_shell()
    shell.display_mode = "last"

    async def hook(*args):
        pass

    shell.hooks[key] = hook
    response = await shell.arun(code)
    assert isinstance(response.exception, TypeError)
    assert "must be synchronous" in str(response.exception)


async def test_async_callable_object_stream_hook():
    seen = []

    class Hook:
        async def __call__(self, data, buffer):
            await asyncio.sleep(0)
            seen.append(data)

    shell = quiet_shell()
    shell.hooks["stdout_hook"] = Hook()
    await shell.arun("print('object hook')")
    assert seen == ["object hook\n"]


async def test_async_hook_worker_persists_between_chunks_without_restarting():
    tasks = []
    first_hook_done = asyncio.Event()
    shell = quiet_shell()

    async def hook(data, buffer):
        tasks.append(asyncio.current_task())
        first_hook_done.set()

    shell.hooks["stdout_hook"] = hook
    shell.update_namespace(first_hook_done=first_hook_done)
    response = await shell.arun("print('one')\nawait first_hook_done.wait()\nprint('two')")
    assert response.exception is None
    assert len(tasks) == 2 and tasks[0] is tasks[1]
    assert tasks[0].done()


async def test_hook_cancellation_propagates_and_does_not_spawn_replacement_worker():
    calls = []
    hook_cancelled = asyncio.Event()
    shell = quiet_shell()

    async def hook(data, buffer):
        calls.append(data)
        hook_cancelled.set()
        raise asyncio.CancelledError()

    shell.hooks["stdout_hook"] = hook
    shell.update_namespace(hook_cancelled=hook_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await shell.arun("print('one')\nawait hook_cancelled.wait()\nprint('two')")
    assert calls == ["one\n"]
    assert shell.run("42").result == 42


async def test_statement_failure_does_not_reuse_preceding_expression_result():
    shell = quiet_shell()
    assert shell.run("42\nraise ValueError()").result is None
    assert (await shell.arun("42\nraise ValueError()")).result is None


async def test_async_silent_capture_skips_async_hook_delivery():
    calls = []
    shell = quiet_shell()

    async def hook(data, buffer):
        calls.append(data)

    shell.hooks["stdout_hook"] = hook
    response = await shell.arun("print('quiet')", silent=True)
    assert response.stdout == "quiet\n"
    assert calls == []


async def test_native_run_preserves_original_magic_input():
    shell = quiet_shell()
    shell.register_magic(lambda text: text.upper(), name="caps")
    code = "%caps hello"
    for response in [shell.run(code), await shell.arun(code)]:
        assert response.exception is None
        assert response.input == code
        assert response.processed_input != code
        assert response.result == "HELLO"


async def test_offloading_blocking_run_is_explicit_and_propagates_context():
    marker = contextvars.ContextVar("offload-marker", default=None)
    token = marker.set("caller")
    shell = quiet_shell()
    shell.update_namespace(marker=marker, threading=threading)
    try:
        response = await asyncio.to_thread(shell.run, "(marker.get(), threading.get_ident())")
        assert response.result[0] == "caller"
        assert response.result[1] != threading.get_ident()
    finally:
        marker.reset(token)


@pytest.mark.parametrize("explicit_first", [False, True])
async def test_explicit_cwd_excludes_other_default_shells(tmp_path, explicit_first):
    entered, release = asyncio.Event(), asyncio.Event()
    first, second = quiet_shell(), quiet_shell()
    first.update_namespace(entered=entered, release=release)
    previous = os.getcwd()
    task = asyncio.create_task(first.arun(
        "entered.set()\nawait release.wait()", **({"cwd": tmp_path} if explicit_first else {})
    ))
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="Concurrent execution requires"):
            await second.arun("pass", **({} if explicit_first else {"cwd": tmp_path}))
    finally:
        release.set()
        await task
        os.chdir(previous)
    assert second.run("42").result == 42
