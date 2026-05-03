"""
Tests de non-régression pour Shell.run() — comportement synchrone.
"""
import pytest
from pynteract import Shell


# ── résultats ──────────────────────────────────────────────────────────────────

class TestRunResult:
    def test_expression_result(self, shell):
        r = shell.run("1 + 1")
        assert r.result == 2

    def test_none_result_on_statement(self, shell):
        r = shell.run("x = 10")
        assert r.result is None

    def test_multi_line_result(self, shell):
        r = shell.run("a = 3\nb = 4\na + b")
        assert r.result == 7

    def test_string_result(self, shell):
        r = shell.run("'hello'")
        assert r.result == "hello"

    def test_list_result(self, shell):
        r = shell.run("[1, 2, 3]")
        assert r.result == [1, 2, 3]


# ── stdout / stderr ────────────────────────────────────────────────────────────

class TestRunOutput:
    def test_stdout_captured(self, shell):
        r = shell.run("print('hi')")
        assert "hi" in r.stdout

    def test_stderr_captured(self, shell):
        r = shell.run("import sys; sys.stderr.write('err\\n')")
        assert "err" in r.stderr

    def test_multiline_print(self, shell):
        r = shell.run("for i in range(3): print(i)")
        assert r.stdout == "0\n1\n2\n"


# ── persistance de namespace ───────────────────────────────────────────────────

class TestNamespacePersistence:
    def test_variable_persists(self, stateful_shell):
        r = stateful_shell.run("x")
        assert r.result == 42

    def test_variable_mutated(self, stateful_shell):
        stateful_shell.run("x = x + 1")
        r = stateful_shell.run("x")
        assert r.result == 43

    def test_function_defined_and_called(self, shell):
        shell.run("def double(n): return n * 2")
        r = shell.run("double(7)")
        assert r.result == 14

    def test_import_persists(self, shell):
        shell.run("import math")
        r = shell.run("math.floor(3.9)")
        assert r.result == 3


# ── gestion des erreurs ────────────────────────────────────────────────────────

class TestRunErrors:
    def test_syntax_error_captured(self, shell):
        # SyntaxError est capturée par le handler de Shell et stockée dans r.exception.
        r = shell.run("def foo(:")
        assert r.exception is not None
        assert isinstance(r.exception, SyntaxError)

    def test_runtime_error_captured(self, shell):
        r = shell.run("1 / 0")
        assert r.exception is not None
        assert isinstance(r.exception, ZeroDivisionError)

    def test_name_error_captured(self, shell):
        r = shell.run("undefined_var")
        assert r.exception is not None
        assert isinstance(r.exception, NameError)

    def test_error_does_not_poison_namespace(self, shell):
        shell.run("y = 99")
        shell.run("1 / 0")
        r = shell.run("y")
        assert r.result == 99

    def test_traceback_enriched(self, shell):
        r = shell.run("raise ValueError('boom')")
        assert r.exception is not None
        assert hasattr(r.exception, "enriched_traceback_string")


# ── hooks synchrones ───────────────────────────────────────────────────────────

class TestSyncHooks:
    def test_stdout_hook_called(self, shell):
        received = []
        shell.hooks["stdout_hook"] = lambda data, buf, ctx: received.append(data)
        shell.run("print('ping')")
        assert any("ping" in chunk for chunk in received)

    def test_stderr_hook_called(self, shell):
        received = []
        shell.hooks["stderr_hook"] = lambda data, buf, ctx: received.append(data)
        shell.run("import sys; sys.stderr.write('oops\\n')")
        assert any("oops" in chunk for chunk in received)

    def test_hook_receives_cumulative_buffer(self, shell):
        buffers = []
        shell.hooks["stdout_hook"] = lambda data, buf, ctx: buffers.append(buf)
        shell.run("print('a')\nprint('b')")
        # cumulative buffer should grow
        assert len(buffers) >= 2
        assert len(buffers[-1]) >= len(buffers[0])

    def test_hook_removal_stops_calls(self, shell):
        received = []
        shell.hooks["stdout_hook"] = lambda data, buf, ctx: received.append(data)
        shell.run("print('first')")
        del shell.hooks["stdout_hook"]
        shell.run("print('second')")
        assert not any("second" in c for c in received)

    def test_shell_silent_default_suppresses_stdio_hooks(self):
        received = []
        shell = Shell(
            silent=True,
            stdout_hook=lambda data, buf, ctx: received.append(data),
        )

        response = shell.run("print('quiet')")

        assert response.stdout == "quiet\n"
        assert received == []

    def test_run_silent_override_can_enable_hooks(self):
        received = []
        shell = Shell(
            silent=True,
            stdout_hook=lambda data, buf, ctx: received.append(data),
        )

        shell.run("print('loud')", silent=False)

        assert any("loud" in chunk for chunk in received)

    def test_run_silent_override_can_suppress_hooks(self):
        received = []
        shell = Shell(stdout_hook=lambda data, buf, ctx: received.append(data))

        response = shell.run("print('quiet')", silent=True)

        assert response.stdout == "quiet\n"
        assert received == []
