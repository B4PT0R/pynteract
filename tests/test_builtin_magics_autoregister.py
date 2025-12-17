from __future__ import annotations

from pynteract import Shell


def test_builtin_magics_are_available_by_default():
    shell = Shell(display_mode="none")
    resp = shell.run("%pwd")
    assert resp.exception is None
    assert isinstance(resp.result, str)

