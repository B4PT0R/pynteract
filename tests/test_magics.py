from __future__ import annotations

from pynteract import Shell


def test_line_magic_executes_registered_function_and_returns_value():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="caps")
    def caps(text: str) -> str:
        return text.upper()

    resp = shell.run("%caps hello")
    assert resp.exception is None
    assert resp.result == "HELLO"


def test_cell_magic_receives_remaining_cell_body():
    shell = Shell(display_mode="none")
    received: list[str] = []

    @shell.register_magic(name="take")
    def take(text: str) -> str:
        received.append(text)
        return "ok"

    resp = shell.run("%%take\nline1\nline2")
    assert resp.exception is None
    assert resp.result == "ok"
    assert received == ["line1\nline2"]


def test_magics_not_parsed_inside_string_or_comment():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="boom")
    def boom(_: str) -> str:
        raise AssertionError("should not be called")

    resp = shell.run("x = '%boom hi'\n# %boom hi\nx")
    assert resp.exception is None
    assert resp.result == "%boom hi"


def test_inline_magic_in_assignment_uses_rhs_as_input():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="caps")
    def caps(text: str) -> str:
        return text.upper()

    resp = shell.run("x = %caps hello world\nx")
    assert resp.exception is None
    assert resp.result == "HELLO WORLD"


def test_inline_magic_after_semicolon_is_parsed():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="caps")
    def caps(text: str) -> str:
        return text.upper()

    resp = shell.run("x = 1; y = %caps hi\ny")
    assert resp.exception is None
    assert resp.result == "HI"


def test_percent_operator_not_misparsed_as_magic():
    shell = Shell(display_mode="none")
    resp = shell.run("10 % 3")
    assert resp.exception is None
    assert resp.result == 1


def test_magic_templates_resolve_brace_expressions():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="upper")
    def upper(text: str) -> str:
        return text.upper()

    resp = shell.run("%upper La liste est {[i for i in range(3)]}")
    assert resp.exception is None
    assert resp.result == "LA LISTE EST [0, 1, 2]"


def test_magic_templates_can_reference_namespace_values():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="upper")
    def upper(text: str) -> str:
        return text.upper()

    resp = shell.run("x = 5\n%upper x={x}")
    assert resp.exception is None
    assert resp.result == "X=5"


def test_magic_templates_support_brace_escaping():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="echo")
    def echo(text: str) -> str:
        return text

    resp = shell.run("%echo {{hello}} {1+1} {{}}")
    assert resp.exception is None
    assert resp.result == "{hello} 2 {}"


def test_line_magic_preserves_semicolon_in_argument():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="echo")
    def echo(text: str) -> str:
        return text

    resp = shell.run("%echo hi;")
    assert resp.exception is None
    assert resp.result == "hi;"


def test_inline_magic_consumes_rest_of_line_including_semicolon():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="echo")
    def echo(text: str) -> str:
        return text

    resp = shell.run("x = %echo hi; y = 1\nx")
    assert resp.exception is None
    assert resp.result == "hi; y = 1"
    assert "y" not in shell.namespace


def test_magic_modes_line_only_and_cell_only():
    shell = Shell(display_mode="none")

    @shell.register_magic(name="lineonly", mode="line")
    def lineonly(text: str) -> str:
        return f"line:{text}"

    @shell.register_magic(name="cellonly", mode="cell")
    def cellonly(text: str) -> str:
        return f"cell:{text}"

    assert shell.run("%lineonly hi").result == "line:hi"
    assert shell.run("%%cellonly\nhi").result == "cell:hi"

    resp = shell.run("%%lineonly\nhi")
    assert isinstance(resp.exception, Exception)

    resp = shell.run("%cellonly hi")
    assert isinstance(resp.exception, Exception)
