from __future__ import annotations

from pynteract import Shell


def test_display_mode_last_only_displays_final_expression():
    seen: list[object] = []

    def display_hook(obj: object, _kwargs: dict, ctx) -> None:
        seen.append(obj)

    shell = Shell(display_mode="last", display_hook=display_hook)
    resp = shell.run("1\n2\n3")
    assert resp.exception is None
    assert resp.result == 3
    assert seen == [3]


def test_display_mode_all_displays_every_expression():
    seen: list[object] = []

    def display_hook(obj: object, _kwargs: dict, ctx) -> None:
        seen.append(obj)

    shell = Shell(display_mode="all", display_hook=display_hook)
    resp = shell.run("1\n2\n3")
    assert resp.exception is None
    assert resp.result == 3
    assert seen == [1, 2, 3]


def test_semicolon_suppresses_display_but_keeps_last_result():
    seen: list[object] = []

    def display_hook(obj: object, _kwargs: dict, ctx) -> None:
        seen.append(obj)

    shell = Shell(display_mode="all", display_hook=display_hook)
    resp = shell.run("1;\n2")
    assert resp.exception is None
    assert resp.result == 2
    assert seen == [2]
    assert shell.namespace["_"] == 2
