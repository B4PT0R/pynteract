import sys

import pytest

from pynteract import Shell


def test_namespace_reference_preserved_and_registered_in_sys_modules():
    ns = {"a": 1}
    shell = Shell(display_mode="none", module_name="__pynteract_test__", namespace=ns)
    assert shell.namespace is ns

    resp = shell.run("b = 2")
    assert resp.exception is None
    assert ns["b"] == 2

    mod = sys.modules["__pynteract_test__"]
    assert mod.__dict__ is ns


def test_future_annotations_persist_across_cells():
    shell = Shell(display_mode="none", module_name="__pynteract_test_future__")

    resp = shell.run("from __future__ import annotations")
    assert resp.exception is None

    resp = shell.run("def f(x: int) -> str:\n    return 'ok'")
    assert resp.exception is None
    assert shell.namespace["f"].__annotations__ == {"x": "int", "return": "str"}


def test_future_import_allowed_after_docstring_position():
    shell = Shell(display_mode="none", module_name="__pynteract_test_docstring__")
    resp = shell.run('"""doc"""\nfrom __future__ import annotations\nx = 1')
    assert resp.exception is None


def test_future_import_rejected_after_statement():
    shell = Shell(display_mode="none", module_name="__pynteract_test_future_err__")
    resp = shell.run("x = 1\nfrom __future__ import annotations")
    assert isinstance(resp.exception, SyntaxError)


def test_run_with_explicit_globals_dict_registers_module_and_sets_dunder_module():
    ns: dict[str, object] = {}
    shell = Shell(display_mode="none", module_name="__pynteract_session__")

    resp = shell.run("class A: pass", globals=ns)
    assert resp.exception is None

    # Class should be created in the provided globals.
    assert "A" in ns
    a = ns["A"]
    assert getattr(a, "__module__") == ns["__name__"]

    # Provided globals are registered as a module-like object.
    assert sys.modules[ns["__name__"]].__dict__ is ns
