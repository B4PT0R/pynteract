from __future__ import annotations

import __main__
import sys
from pathlib import Path

from pynteract.cli import build_shell, main


def test_build_shell_uses_main_module_namespace():
    shell = build_shell()
    assert shell.namespace is not None
    assert shell.namespace is __main__.__dict__


def test_cli_main_no_interact_exits_cleanly():
    assert main(["--no-interact"]) == 0


def test_cli_runs_script_and_restores_main_module(tmp_path: Path):
    script = tmp_path / "script.py"
    script.write_text("x = 123\n", encoding="utf-8")

    saved_main = sys.modules.get("__main__")
    saved_argv = sys.argv[:]
    code = main(["--no-interact", str(script)])
    assert code == 0
    assert sys.modules.get("__main__") is saved_main
    assert sys.argv == saved_argv
