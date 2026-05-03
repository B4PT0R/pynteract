"""Tests de non-régression pour magics.py."""
import threading
import contextvars
import pytest
from pynteract.magics import MagicParser, Magic, _PLACEHOLDER_STORE


# ── Magic dataclass ────────────────────────────────────────────────────────────

class TestMagic:
    def test_callable(self):
        m = Magic(func=lambda text: text.upper())
        assert m("hello") == "HELLO"

    def test_default_mode(self):
        m = Magic(func=lambda t: t)
        assert m.mode == "both"

    def test_custom_mode(self):
        m = Magic(func=lambda t: t, mode="cell")
        assert m.mode == "cell"

    def test_frozen(self):
        m = Magic(func=lambda t: t)
        with pytest.raises((AttributeError, TypeError)):
            m.mode = "line"  # type: ignore


# ── _render_template — safe eval ──────────────────────────────────────────────

class TestRenderTemplate:
    def setup_method(self, _):
        self.p = MagicParser()

    def test_literal_substitution(self):
        result = self.p._render_template("{x}", {"x": 42}, {})
        assert result == "42"

    def test_escaped_braces(self):
        result = self.p._render_template("{{literal}}", {}, {})
        assert result == "{literal}"

    def test_expression(self):
        result = self.p._render_template("{a + b}", {"a": 3, "b": 4}, {})
        assert result == "7"

    def test_locals_override_globals(self):
        result = self.p._render_template("{x}", {"x": 1}, {"x": 99})
        assert result == "99"

    def test_no_substitution(self):
        assert self.p._render_template("hello world", {}, {}) == "hello world"

    def test_unmatched_open_brace_raises(self):
        with pytest.raises(ValueError, match="Unmatched"):
            self.p._render_template("{x", {"x": 1}, {})

    def test_unmatched_close_brace_raises(self):
        with pytest.raises(ValueError, match="Unmatched"):
            self.p._render_template("x}", {}, {})

    def test_empty_expression_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            self.p._render_template("{}", {}, {})

    # -- sandbox --

    def test_blocks_import(self):
        with pytest.raises(NameError):
            self.p._render_template("{__import__('os')}", {}, {})

    def test_blocks_builtins_escape(self):
        # La sandbox remplace __builtins__ par un dict whitelist sans '__import__'.
        # Tenter d'y accéder lève KeyError (clé absente) — le vecteur d'évasion est bloqué.
        with pytest.raises((NameError, TypeError, KeyError)):
            self.p._render_template(
                "{__builtins__['__import__']('os')}", {}, {}
            )

    def test_allows_safe_builtins(self):
        assert self.p._render_template("{len('hello')}", {}, {}) == "5"
        assert self.p._render_template("{str(42)}", {}, {}) == "42"
        assert self.p._render_template("{max(1,2,3)}", {}, {}) == "3"

    def test_user_vars_still_accessible(self):
        result = self.p._render_template("{secret}", {"secret": "visible"}, {})
        assert result == "visible"


# ── _placeholder_scope — isolation ────────────────────────────────────────────

class TestPlaceholderScope:
    def setup_method(self, _):
        self.p = MagicParser()

    def test_scope_starts_empty(self):
        with self.p._placeholder_scope() as store:
            assert store == {}

    def test_scope_isolates_placeholders(self):
        with self.p._placeholder_scope() as s1:
            s1["k1"] = "v1"
            with self.p._placeholder_scope() as s2:
                s2["k2"] = "v2"
                assert "k1" not in s2
            # outer scope restored
            assert self.p._current_placeholders() == {"k1": "v1"}

    def test_scope_cleaned_after_exception(self):
        try:
            with self.p._placeholder_scope() as store:
                store["x"] = "boom"
                raise RuntimeError("oops")
        except RuntimeError:
            pass
        # After scope exits the store is reset (outer scope, default empty)
        assert "x" not in self.p._current_placeholders()

    def test_concurrent_scopes_isolated(self):
        """Two threads using _placeholder_scope must not see each other's keys."""
        errors = []
        seen = {}

        def worker(name, key, value, barrier):
            with self.p._placeholder_scope() as store:
                store[key] = value
                barrier.wait()          # both threads in scope simultaneously
                snap = dict(self.p._current_placeholders())
                seen[name] = snap

        b = threading.Barrier(2)
        t1 = threading.Thread(target=worker, args=("t1", "key_a", "val_a", b))
        t2 = threading.Thread(target=worker, args=("t2", "key_b", "val_b", b))
        t1.start(); t2.start()
        t1.join();  t2.join()

        assert not errors
        assert "key_b" not in seen["t1"], "t1 saw t2's placeholder"
        assert "key_a" not in seen["t2"], "t2 saw t1's placeholder"


# ── _build_ignore_map ──────────────────────────────────────────────────────────

class TestBuildIgnoreMap:
    def setup_method(self, _):
        self.p = MagicParser()

    def test_string_ignored(self):
        code = '"hello %magic"'
        lines = code.split("\n")
        imap = self.p._build_ignore_map(code, lines)
        # The % at col 7 must be inside the ignored span
        assert self.p._position_ignored(imap, 1, 7)

    def test_comment_ignored(self):
        code = "x = 1  # %magic"
        lines = code.split("\n")
        imap = self.p._build_ignore_map(code, lines)
        assert self.p._position_ignored(imap, 1, 10)

    def test_code_not_ignored(self):
        code = "%timeit x"
        lines = code.split("\n")
        imap = self.p._build_ignore_map(code, lines)
        assert not self.p._position_ignored(imap, 1, 0)

    def test_incomplete_code_returns_empty(self):
        # list() force l'évaluation complète du générateur dans le try/except —
        # TokenError est maintenant toujours attrapée, même en Python 3.12+.
        imap = self.p._build_ignore_map("def foo(:", ["def foo(:"])
        assert isinstance(imap, dict)
        assert imap == {}


# ── _parse_system_cmd ─────────────────────────────────────────────────────────

class TestParseSystemCmd:
    def setup_method(self, _):
        self.p = MagicParser()

    def _parse(self, code):
        with self.p._placeholder_scope():
            return self.p._parse_system_cmd(code)

    def test_single_bang(self):
        result = self._parse("!ls -la")
        assert "run_system_cmd(" in result
        assert "!ls" not in result

    def test_double_bang_capture(self):
        result = self._parse("!!ls")
        assert "run_system_cmd_capture(" in result

    def test_bang_in_string_not_transformed(self):
        code = 'x = "!not a command"'
        result = self._parse(code)
        assert result == code

    def test_indented_bang(self):
        code = "if True:\n    !echo hi"
        result = self._parse(code)
        assert "    __shell__.run_system_cmd(" in result

    def test_placeholder_key_in_result(self):
        result = self._parse("!echo test")
        assert "_render_placeholder(" in result


# ── _parse_magics ──────────────────────────────────────────────────────────────

class TestParseMagics:
    def setup_method(self, _):
        self.p = MagicParser()

    def _parse(self, code):
        with self.p._placeholder_scope():
            return self.p._parse_magics(code)

    def test_line_magic(self):
        result = self._parse("%timeit x + 1")
        assert "_call_magic('timeit', 'line'" in result

    def test_cell_magic(self):
        result = self._parse("%%bash\necho hello")
        assert "_call_magic('bash', 'cell'" in result

    def test_magic_in_string_not_transformed(self):
        code = 'x = "%not_magic"'
        result = self._parse(code)
        assert result == code

    def test_inline_magic(self):
        result = self._parse("x = %mymagic arg")
        assert "_call_magic('mymagic', 'line'" in result

    def test_double_percent_not_inline(self):
        # %% at start of cell is cell magic, not inline
        result = self._parse("%%bash\necho hi")
        assert "_call_magic('bash', 'cell'" in result

    def test_plain_code_unchanged(self):
        code = "x = 1 + 2"
        assert self._parse(code) == code
