from __future__ import annotations

from pynteract import Shell


def test_reset_namespace_clears_user_symbols_and_future_flags():
    shell = Shell(display_mode="none")

    shell.run("from __future__ import annotations\ndef f(x: int) -> str: ...")
    assert shell.namespace["f"].__annotations__ == {"x": "int", "return": "str"}

    shell.reset_namespace()
    assert "f" not in shell.namespace

    shell.run("def g(x: int) -> str: ...")
    # Without the future flag, annotations evaluate to real objects.
    assert shell.namespace["g"].__annotations__ == {"x": int, "return": str}

