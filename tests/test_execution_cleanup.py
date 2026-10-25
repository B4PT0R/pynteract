"""Host process state must survive failures in user code and hooks."""
import asyncio
import os
import sys

import pytest

from pynteract import RunContext, Shell, get_run_ctx
from pynteract.collector import Collector
from pynteract.context import CAPTURE_STREAMS, PENDING_STDIN_PROMPT, SILENT_STDIO


def test_get_run_ctx_outside_execution():
    assert get_run_ctx() is None
    assert RunContext(name="cell").name == "cell"


def test_context_available_in_every_hook():
    seen = {}

    def observe(name, value=None):
        def hook(*args):
            seen[name] = get_run_ctx()
            return value(*args) if value else None
        return hook

    shell = Shell(
        input_hook=observe("input"),
        pre_run_hook=observe("pre_run", lambda code: code),
        code_block_hook=observe("code_block"),
        pre_execute_hook=observe("pre_execute", lambda node, source: node),
        post_execute_hook=observe("post_execute"),
        display_hook=observe("display"),
        stdout_hook=observe("stdout"),
        stderr_hook=observe("stderr"),
        stdin_hook=observe("stdin", lambda: "answer"),
        exception_hook=observe("exception"),
        namespace_change_hook=observe("namespace_change"),
        post_run_hook=observe("post_run", lambda response: response),
    )
    response = shell.run(
        "answer = input('prompt: ')\nprint(answer)\n42\nraise ValueError('error')",
        filename="cell",
    )
    assert isinstance(response.exception, ValueError)
    assert set(seen) == {name.removesuffix("_hook") for name in shell.hooks} - {"display"}
    shell.run("42", filename="cell")
    assert "display" in seen
    assert all(ctx == RunContext(name="cell") for ctx in seen.values())
    assert get_run_ctx() is None


@pytest.mark.parametrize("hook_name", ["input_hook", "pre_run_hook", "post_run_hook"])
def test_context_and_cwd_restored_after_host_hook_failure(hook_name, tmp_path):
    original = os.getcwd()

    def fail(*args):
        assert get_run_ctx().name == "failing-cell"
        raise RuntimeError("hook failed")

    shell = Shell(use_internal_cwd=True, **{hook_name: fail})
    with pytest.raises(RuntimeError, match="hook failed"):
        shell.run("pass", cwd=tmp_path, filename="failing-cell", silent=True)
    assert os.getcwd() == original
    assert get_run_ctx() is None
    assert CAPTURE_STREAMS.get() is None
    assert not SILENT_STDIO.get()
    assert not PENDING_STDIN_PROMPT.get()


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_final_flush_failure_restores_all_streams(stream_name):
    saved = sys.stdout, sys.stderr, sys.stdin

    def fail(data, buffer):
        raise RuntimeError("flush failed")

    shell = Shell(**{stream_name + "_hook": fail})
    token = PENDING_STDIN_PROMPT.set(False)
    try:
        with pytest.raises(RuntimeError, match="flush failed"):
            with Collector(shell):
                getattr(sys, stream_name).write("unfinished line")
        assert (sys.stdout, sys.stderr, sys.stdin) == saved
    finally:
        PENDING_STDIN_PROMPT.reset(token)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(17), asyncio.CancelledError()])
def test_control_flow_propagates_and_restores_state(error, tmp_path):
    saved = sys.stdout, sys.stderr, sys.stdin
    original_cwd = os.getcwd()
    shell = Shell(namespace={"error": error}, use_internal_cwd=True)
    with pytest.raises(type(error)) as caught:
        shell.run("raise error", cwd=tmp_path)
    assert caught.value is error
    assert (sys.stdout, sys.stderr, sys.stdin) == saved
    assert os.getcwd() == original_cwd
    assert get_run_ctx() is None
    assert CAPTURE_STREAMS.get() is None


def test_context_reset_even_when_cwd_restore_fails(monkeypatch, tmp_path):
    original_chdir = os.chdir
    original_cwd = os.getcwd()
    calls = 0

    def chdir(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("restore failed")
        original_chdir(path)

    shell = Shell(use_internal_cwd=True)
    monkeypatch.setattr(os, "chdir", chdir)
    try:
        with pytest.raises(OSError, match="restore failed"):
            shell.run("pass", cwd=tmp_path, silent=True)
        assert get_run_ctx() is None
        assert not SILENT_STDIO.get()
        assert not PENDING_STDIN_PROMPT.get()
    finally:
        original_chdir(original_cwd)


def test_display_outside_run_has_no_context():
    seen = []
    shell = Shell(display_hook=lambda obj, kwargs: seen.append((obj, kwargs, get_run_ctx())))
    shell.display(42, backend="text")
    assert seen == [(42, {"backend": "text"}, None)]
