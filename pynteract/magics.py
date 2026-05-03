from .utils import short_id
from contextlib import contextmanager
import contextvars
import tokenize
import io
import re
from dataclasses import dataclass
from typing import Callable, Literal, Any

# ---------------------------------------------------------------------------
# Per-execution placeholder store — ContextVar for full thread + async safety
# ---------------------------------------------------------------------------
# Each shell.run() / shell.arun() call pushes a fresh dict onto this var via
# _placeholder_scope().  Concurrent executions on different threads (arun with
# ThreadPoolExecutor) or nested runs (a magic that calls __shell__.run) each
# see their own isolated store without any locking needed.
_PLACEHOLDER_STORE: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "_placeholder_store", default={}
)

# Built-ins that are explicitly allowed in magic template expressions.
# Everything else is blocked to prevent arbitrary code injection via
# template strings like `{__import__('os').system('rm -rf /')}`.
_TEMPLATE_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
    "bytes": bytes, "chr": chr, "dict": dict, "dir": dir, "divmod": divmod,
    "enumerate": enumerate, "filter": filter, "float": float, "format": format,
    "frozenset": frozenset, "getattr": getattr, "hasattr": hasattr, "hash": hash,
    "hex": hex, "int": int, "isinstance": isinstance, "issubclass": issubclass,
    "iter": iter, "len": len, "list": list, "map": map, "max": max, "min": min,
    "next": next, "oct": oct, "ord": ord, "pow": pow, "print": print,
    "range": range, "repr": repr, "reversed": reversed, "round": round,
    "set": set, "slice": slice, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
    "True": True, "False": False, "None": None,
}


@dataclass(frozen=True, slots=True)
class Magic:
    func: Callable[[str], Any]
    mode: Literal["line", "cell", "both"] = "both"

    def __call__(self, text: str) -> Any:
        return self.func(text)


class MagicParser:

    def __init__(self):
        # _placeholders is kept as an instance attribute for backward compat
        # with code that reads it directly, but all write paths go through
        # _current_placeholders() which reads from the ContextVar.
        self._placeholders: dict[str, str] = {}

    _INLINE_MAGIC_RE = re.compile(r"%(?P<name>[A-Za-z_][A-Za-z0-9_]*)")

    # ------------------------------------------------------------------
    # Placeholder store — thread + async safe via ContextVar
    # ------------------------------------------------------------------

    @staticmethod
    def _current_placeholders() -> dict[str, str]:
        """Return the placeholder dict for the current execution context."""
        return _PLACEHOLDER_STORE.get()

    @contextmanager
    def _placeholder_scope(self):
        """Isolated placeholder store for a single shell.run() call.

        Uses a ``ContextVar`` so that:
        - Concurrent ``arun()`` calls on different threads each get their own store.
        - Nested ``shell.run()`` calls (e.g. a magic that calls ``__shell__.run``)
          get a fresh child store, restoring the parent on exit.

        The instance attribute ``self._placeholders`` is kept as a proxy to the
        active store for any code that reads it directly.
        """
        fresh: dict[str, str] = {}
        token = _PLACEHOLDER_STORE.set(fresh)
        self._placeholders = fresh
        try:
            yield fresh
        finally:
            _PLACEHOLDER_STORE.reset(token)
            # Restore instance attr to whatever the parent scope had
            self._placeholders = _PLACEHOLDER_STORE.get()

    # ------------------------------------------------------------------
    # Template rendering — restricted eval
    # ------------------------------------------------------------------

    @staticmethod
    def _render_template(text: str, globals_dict: dict, locals_dict: dict) -> str:
        """IPython-style template expansion with a restricted eval sandbox.

        - ``{expr}`` is evaluated and substituted with ``str(value)``.
        - ``{{`` and ``}}`` escape literal braces.

        The eval sandbox merges ``globals_dict`` and ``locals_dict`` as usual,
        but overrides ``__builtins__`` with a curated whitelist
        (:data:`_TEMPLATE_SAFE_BUILTINS`) so that dangerous calls like
        ``{__import__('os').system('rm -rf /')}`` are blocked with a
        ``NameError`` rather than silently executed.

        Note: the user's own namespace variables (globals_dict / locals_dict)
        are still fully accessible — only the *built-in* surface is restricted.
        """
        out: list[str] = []
        i = 0
        n = len(text)

        # Build a safe globals that restricts builtins but preserves user vars.
        safe_globals = dict(globals_dict)
        safe_globals["__builtins__"] = _TEMPLATE_SAFE_BUILTINS

        while i < n:
            ch = text[i]
            if ch == "{":
                if i + 1 < n and text[i + 1] == "{":
                    out.append("{")
                    i += 2
                    continue
                j = text.find("}", i + 1)
                if j == -1:
                    raise ValueError("Unmatched '{' in template")
                expr = text[i + 1 : j].strip()
                if not expr:
                    raise ValueError("Empty '{}' template expression")
                value = eval(expr, safe_globals, locals_dict)  # noqa: S307
                out.append(str(value))
                i = j + 1
                continue
            if ch == "}":
                if i + 1 < n and text[i + 1] == "}":
                    out.append("}")
                    i += 2
                    continue
                raise ValueError("Unmatched '}' in template")
            out.append(ch)
            i += 1
        return "".join(out)

    def _render_placeholder(self, key: str, globals_dict: dict, locals_dict: dict) -> str:
        raw = self._current_placeholders().get(key, "")
        return self._render_template(raw, globals_dict, locals_dict)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _prev_non_space_char(line: str, idx: int) -> str | None:
        j = idx - 1
        while j >= 0 and line[j].isspace():
            j -= 1
        return line[j] if j >= 0 else None

    def _find_inline_magic(self, line: str, line_no: int, ignore_map) -> tuple[int, str, str] | None:
        """Return (column, magic_name, rhs_text) for the first inline magic in *line*, if any."""
        allowed_prev = {"=", "(", "[", "{", ",", ":", ";"}
        for match in self._INLINE_MAGIC_RE.finditer(line):
            idx = match.start()
            if idx > 0 and line[idx - 1] == "%":
                continue
            if self._position_ignored(ignore_map, line_no, idx):
                continue
            prev = self._prev_non_space_char(line, idx)
            if prev is not None and prev not in allowed_prev:
                continue
            magic = match.group("name")
            rhs = line[match.end():].lstrip()
            return idx, magic, rhs
        return None

    def _parse_system_cmd(self, code: str) -> str:
        """Transform shell-escape syntax (``!cmd``, ``!!cmd``) into __shell__ calls."""
        store = self._current_placeholders()
        stripped_code = code.lstrip("\n")
        if stripped_code.startswith("!!"):
            command = stripped_code[2:].lstrip("\n")
            pid = short_id()
            store[pid] = command
            return (
                f"__shell__.run_system_cmd_capture("
                f"__shell__._magic_parser._render_placeholder('{pid}', globals(), locals()))"
            )

        lines = code.split("\n")
        ignore_map = self._build_ignore_map(code, lines)
        for i, line in enumerate(lines):
            stripped_line = line.lstrip()
            if stripped_line.startswith("!") and not stripped_line.startswith("!!"):
                column = len(line) - len(stripped_line)
                if self._position_ignored(ignore_map, i + 1, column):
                    continue
                indent = line[:column]
                command = stripped_line[1:].lstrip()
                pid = short_id()
                store[pid] = command
                lines[i] = (
                    f"{indent}__shell__.run_system_cmd("
                    f"__shell__._magic_parser._render_placeholder('{pid}', globals(), locals()))"
                )
        return "\n".join(lines)

    def _parse_magics(self, code: str) -> str:
        """Transform ``%magic`` / ``%%magic`` syntax into __shell__ calls."""
        store = self._current_placeholders()
        stripped_code = code.lstrip("\n")
        if stripped_code.startswith("%%"):
            lines = stripped_code.split("\n")
            magic = lines[0][2:].strip()
            content = "\n".join(lines[1:])
            pid = short_id()
            store[pid] = content
            return (
                f"__shell__._call_magic('{magic}', 'cell', "
                f"__shell__._magic_parser._render_placeholder('{pid}', globals(), locals()))"
            )

        lines = code.split("\n")
        ignore_map = self._build_ignore_map(code, lines)
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("%"):
                column = len(line) - len(stripped)
                if self._position_ignored(ignore_map, i + 1, column):
                    continue
                indent = line[:column]
                parts = stripped.split(" ", 1)
                magic = parts[0][1:]
                content = parts[1].strip() if len(parts) > 1 else ""
                pid = short_id()
                store[pid] = content
                lines[i] = (
                    f"{indent}__shell__._call_magic('{magic}', 'line', "
                    f"__shell__._magic_parser._render_placeholder('{pid}', globals(), locals()))"
                )
                continue

            inline = self._find_inline_magic(line, i + 1, ignore_map)
            if inline is None:
                continue
            column, magic, content = inline
            pid = short_id()
            store[pid] = content
            left = line[:column]
            replacement = (
                f"__shell__._call_magic('{magic}', 'line', "
                f"__shell__._magic_parser._render_placeholder('{pid}', globals(), locals()))"
            )
            lines[i] = f"{left}{replacement}"
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Ignore map
    # ------------------------------------------------------------------

    @staticmethod
    def _position_ignored(ignore_map, line_no, column) -> bool:
        """Return True if *column* on *line_no* falls inside a string or comment."""
        for start, end in ignore_map.get(line_no, ()):
            if start <= column < end:
                return True
        return False

    def _build_ignore_map(self, code: str, lines: list[str]) -> dict:
        """Build a map of token spans to ignore (strings and comments).

        Used to avoid mis-parsing ``%``, ``!``, ``{`` inside string literals or
        comments.  Returns an empty dict on tokenization errors (e.g. incomplete
        syntax) so the caller degrades gracefully.
        """
        ignore: dict[int, list[tuple[int, int]]] = {}
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return ignore

        for tok in tokens:
            if tok.type not in (tokenize.STRING, tokenize.COMMENT):
                continue
            (start_line, start_col) = tok.start
            (end_line, end_col) = tok.end

            if start_line == end_line:
                ignore.setdefault(start_line, []).append((start_col, end_col))
            else:
                line_text = lines[start_line - 1] if start_line - 1 < len(lines) else ""
                ignore.setdefault(start_line, []).append((start_col, len(line_text)))
                for ln in range(start_line + 1, end_line):
                    line_text = lines[ln - 1] if ln - 1 < len(lines) else ""
                    ignore.setdefault(ln, []).append((0, len(line_text)))
                ignore.setdefault(end_line, []).append((0, end_col))

        return ignore
