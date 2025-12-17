from __future__ import annotations

import sys

from pynteract import Shell
from pynteract.builtin_magics import register_builtin_magics


def test_run_executes_script_and_merges_into_current_namespace(tmp_path):
    script = tmp_path / "script.py"
    script.write_text("x = 123\n", encoding="utf-8")

    shell = Shell(display_mode="none")
    register_builtin_magics(shell)
    shell.run(f"%run {script}")

    assert shell.namespace["x"] == 123


def test_run_passes_argv_and_restores_sys_argv(tmp_path):
    script = tmp_path / "argv_script.py"
    script.write_text("import sys\narg = sys.argv[1]\n", encoding="utf-8")

    saved = sys.argv[:]
    shell = Shell(display_mode="none")
    register_builtin_magics(shell)
    shell.run(f"%run {script} hello")

    assert shell.namespace["arg"] == "hello"
    assert sys.argv == saved


def test_run_i_executes_in_current_namespace(tmp_path):
    script = tmp_path / "uses_current_ns.py"
    script.write_text("z = y + 1\n", encoding="utf-8")

    shell = Shell(display_mode="none")
    register_builtin_magics(shell)
    shell.namespace["y"] = 10

    shell.run(f"%run -i {script}")
    assert shell.namespace["z"] == 11

