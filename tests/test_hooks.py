from __future__ import annotations

import ast

from pynteract import Shell


def test_basic_hooks_flow_and_mutation():
    calls: list[tuple[str, object]] = []

    def input_hook(code: str, ctx) -> None:
        calls.append(("input", code))

    def pre_run_hook(code: str, ctx) -> str:
        calls.append(("pre_run", code))
        return code.replace("VALUE", "41 + 1")

    def code_block_hook(block: str, ctx) -> None:
        calls.append(("block", block.strip()))

    def pre_execute_hook(node: ast.AST, source: str, ctx) -> ast.AST:
        calls.append(("pre_execute", type(node).__name__))
        return node

    def post_execute_hook(node: ast.AST, result: object, ctx) -> None:
        calls.append(("post_execute", (type(node).__name__, result)))

    def display_hook(obj: object, _kwargs: dict, ctx) -> None:
        calls.append(("display", obj))

    def post_run_hook(resp, ctx):
        calls.append(("post_run", resp.result))
        resp.processed_input = (resp.processed_input or "") + "\n# post_run"
        return resp

    shell = Shell(
        display_mode="last",
        input_hook=input_hook,
        pre_run_hook=pre_run_hook,
        code_block_hook=code_block_hook,
        pre_execute_hook=pre_execute_hook,
        post_execute_hook=post_execute_hook,
        display_hook=display_hook,
        post_run_hook=post_run_hook,
    )

    resp = shell.run("x = VALUE\nx")
    assert resp.exception is None
    assert resp.result == 42
    assert resp.processed_input is not None and resp.processed_input.endswith("# post_run")

    # sanity: the hooks were called in a sensible order
    names = [c[0] for c in calls]
    assert names[0] == "input"
    assert "pre_run" in names
    assert "pre_execute" in names
    assert "post_execute" in names
    assert ("display", 42) in calls
    assert ("post_run", 42) in calls


def test_namespace_change_hook_sees_diff():
    diffs: list[tuple[set[str], set[str]]] = []

    def namespace_change_hook(old, new, _locals, ctx):
        diffs.append((set(old.keys()), set(new.keys())))

    shell = Shell(display_mode="none", namespace_change_hook=namespace_change_hook)
    shell.run("x = 1")
    assert diffs
    before, after = diffs[-1]
    assert "x" not in before
    assert "x" in after
