from __future__ import annotations

import os
from pathlib import Path

import pytest

from pynteract import Shell


@pytest.mark.parametrize("change_code", ["import os; os.chdir({target!r})", "%cd {target}"])
def test_run_uses_internal_cwd_and_restores_process_cwd(tmp_path: Path, change_code: str):
    start = tmp_path / "start"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    start.mkdir()
    target.mkdir()
    outside.mkdir()

    shell = Shell(display_mode="none")
    shell.cwd = str(start)

    previous = os.getcwd()
    try:
        os.chdir(outside)
        resp = shell.run("import os\nprint(os.getcwd())\n" + change_code.format(target=str(target)))
        assert resp.exception is None
        assert str(start) in resp.stdout
        assert os.getcwd() == str(outside)
        assert shell.cwd == str(target)

        resp = shell.run("import os\nos.getcwd()")
        assert resp.exception is None
        assert resp.result == str(target)
        assert os.getcwd() == str(outside)
    finally:
        os.chdir(previous)


def test_run_cwd_argument_sets_shell_cwd_for_next_runs(tmp_path: Path):
    start = tmp_path / "start"
    other = tmp_path / "other"
    outside = tmp_path / "outside"
    start.mkdir()
    other.mkdir()
    outside.mkdir()

    shell = Shell(display_mode="none")

    previous = os.getcwd()
    try:
        os.chdir(outside)
        resp = shell.run("import os\nos.getcwd()", cwd=start)
        assert resp.exception is None
        assert resp.result == str(start)
        assert shell.cwd == str(start)
        assert os.getcwd() == str(outside)

        resp = shell.run(f"import os\nos.chdir({str(other)!r})")
        assert resp.exception is None
        assert shell.cwd == str(other)
        assert os.getcwd() == str(outside)
    finally:
        os.chdir(previous)


@pytest.mark.asyncio
async def test_arun_accepts_cwd_and_restores_process_cwd(tmp_path: Path):
    start = tmp_path / "start"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    start.mkdir()
    target.mkdir()
    outside.mkdir()

    shell = Shell(display_mode="none")

    previous = os.getcwd()
    try:
        os.chdir(outside)
        resp = await shell.arun(f"import os\nprint(os.getcwd())\nos.chdir({str(target)!r})", cwd=start)
        assert resp.exception is None
        assert str(start) in resp.stdout
        assert shell.cwd == str(target)
        assert os.getcwd() == str(outside)
    finally:
        os.chdir(previous)
