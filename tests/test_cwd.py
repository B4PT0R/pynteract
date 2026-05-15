from __future__ import annotations

import os
from pathlib import Path

import pytest

from pynteract import Shell


@pytest.mark.parametrize("change_code", ["import os; os.chdir({target!r})", "%cd {target}"])
def test_internal_cwd_mode_restores_process_cwd(tmp_path: Path, change_code: str):
    start = tmp_path / "start"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    start.mkdir()
    target.mkdir()
    outside.mkdir()

    shell = Shell(display_mode="none", use_internal_cwd=True)
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


def test_internal_cwd_argument_sets_shell_cwd_only(tmp_path: Path):
    start = tmp_path / "start"
    other = tmp_path / "other"
    outside = tmp_path / "outside"
    start.mkdir()
    other.mkdir()
    outside.mkdir()

    shell = Shell(display_mode="none", use_internal_cwd=True)

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


def test_ambient_cwd_mode_uses_and_mutates_process_cwd(tmp_path: Path):
    start = tmp_path / "start"
    target = tmp_path / "target"
    start.mkdir()
    target.mkdir()

    shell = Shell(display_mode="none", use_internal_cwd=False)

    previous = os.getcwd()
    try:
        os.chdir(start)
        resp = shell.run(f"import os\nprint(os.getcwd())\nos.chdir({str(target)!r})")
        assert resp.exception is None
        assert str(start) in resp.stdout
        assert os.getcwd() == str(target)
        assert shell.cwd == str(target)
    finally:
        os.chdir(previous)


def test_ambient_cwd_argument_mutates_process_cwd(tmp_path: Path):
    start = tmp_path / "start"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    start.mkdir()
    target.mkdir()
    outside.mkdir()

    shell = Shell(display_mode="none", use_internal_cwd=False)

    previous = os.getcwd()
    try:
        os.chdir(outside)
        resp = shell.run(f"import os\nprint(os.getcwd())\nos.chdir({str(target)!r})", cwd=start)
        assert resp.exception is None
        assert str(start) in resp.stdout
        assert os.getcwd() == str(target)
        assert shell.cwd == str(target)
    finally:
        os.chdir(previous)


def test_ambient_cwd_property_sets_process_cwd(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    shell = Shell(display_mode="none", use_internal_cwd=False)

    previous = os.getcwd()
    try:
        shell.cwd = target
        assert os.getcwd() == str(target)
        assert shell.cwd == str(target)
    finally:
        os.chdir(previous)


@pytest.mark.asyncio
async def test_arun_internal_cwd_accepts_cwd_and_restores_process_cwd(tmp_path: Path):
    start = tmp_path / "start"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    start.mkdir()
    target.mkdir()
    outside.mkdir()

    shell = Shell(display_mode="none", use_internal_cwd=True)

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


@pytest.mark.asyncio
async def test_arun_ambient_cwd_accepts_cwd_and_mutates_process_cwd(tmp_path: Path):
    start = tmp_path / "start"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    start.mkdir()
    target.mkdir()
    outside.mkdir()

    shell = Shell(display_mode="none", use_internal_cwd=False)

    previous = os.getcwd()
    try:
        os.chdir(outside)
        resp = await shell.arun(f"import os\nprint(os.getcwd())\nos.chdir({str(target)!r})", cwd=start)
        assert resp.exception is None
        assert str(start) in resp.stdout
        assert shell.cwd == str(target)
        assert os.getcwd() == str(target)
    finally:
        os.chdir(previous)
