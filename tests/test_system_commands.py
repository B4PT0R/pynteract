from __future__ import annotations

import sys

import pytest

from pynteract import Shell


@pytest.mark.skipif(sys.platform.startswith("win"), reason="system command tests assume a POSIX-like shell")
def test_bang_command_runs_and_streams_stdout():
    shell = Shell(display_mode="none")
    resp = shell.run('!python3 -c "print(123)"')
    assert resp.exception is None
    assert "123" in (resp.stdout or "")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="system command tests assume a POSIX-like shell")
def test_double_bang_command_is_supported():
    shell = Shell(display_mode="none")
    resp = shell.run('!!python3 -c "print(456)"')
    assert resp.exception is None
    assert "456" in (resp.result or "")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="system command tests assume a POSIX-like shell")
def test_system_command_templates_resolve_brace_expressions():
    shell = Shell(display_mode="none")
    resp = shell.run('x = 7\n!python3 -c "print({x})"')
    assert resp.exception is None
    assert "7" in (resp.stdout or "")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="system command tests assume a POSIX-like shell")
def test_double_bang_stderr_is_streamed_but_stdout_is_result():
    shell = Shell(display_mode="none")
    resp = shell.run('!!python3 -c "import sys; print(1); print(2, file=sys.stderr)"')
    assert resp.exception is None
    assert "1" in (resp.result or "")
    assert "2" in (resp.stderr or "")
