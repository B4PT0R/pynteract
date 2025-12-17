from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pynteract import Shell
from pynteract.builtin_magics import register_builtin_magics


def test_register_builtin_magics_basic_pwd_cd_env_who(tmp_path: Path):
    shell = Shell(display_mode="none")
    register_builtin_magics(shell)

    resp = shell.run("%pwd")
    assert resp.exception is None
    assert resp.result == os.getcwd()

    resp = shell.run(f"%cd {tmp_path}")
    assert resp.exception is None
    assert resp.result == str(tmp_path)

    resp = shell.run("%env FOO=bar")
    assert resp.exception is None
    assert resp.result == "bar"

    resp = shell.run("%env FOO")
    assert resp.exception is None
    assert resp.result == "bar"

    shell.run("x = 123")
    resp = shell.run("%who")
    assert resp.exception is None
    assert "x" in (resp.result or "")


def test_time_and_timeit_return_strings():
    shell = Shell(display_mode="none")
    register_builtin_magics(shell)

    resp = shell.run("%time 1+1")
    assert resp.exception is None
    assert "Wall time:" in (resp.result or "")

    resp = shell.run("%timeit -n 10 -r 1 1+1")
    assert resp.exception is None
    assert "per loop" in (resp.result or "")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="%%bash uses bash")
def test_bash_cell_magic_runs_script():
    shell = Shell(display_mode="none")
    register_builtin_magics(shell)
    resp = shell.run("%%bash\necho hi")
    assert resp.exception is None
    assert "hi" in (resp.stdout or "")
    assert resp.result == 0
