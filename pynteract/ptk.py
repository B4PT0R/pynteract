from __future__ import annotations

import os
from typing import Any, Optional, Tuple, Literal

from . import user_config

_PTK_SHARED: dict[str, Any] | None = None
_PTK_SESSIONS: dict[str, "PtkSession"] = {}


class PtkSession:
    def __init__(
        self,
        *,
        highlighting: Literal["text", "python"],
        key_bindings: Any,
        clipboard: Any,
        history: Any,
        prompt_session_cls: Any,
    ) -> None:
        self._highlighting = highlighting
        self._lexer = _build_lexer(highlighting)
        self._session = prompt_session_cls(key_bindings=key_bindings, clipboard=clipboard, history=history)

    def prompt(
        self,
        *,
        message: str,
        multiline: bool,
        prompt_continuation: str,
        wrap_lines: bool,
    ) -> str:
        try:
            from prompt_toolkit.patch_stdout import patch_stdout
        except ImportError as exc:
            raise RuntimeError(
                "prompt_toolkit is required for terminal interactive shell mode. "
                "Install it via 'pip install \"pynteract[terminal]\"' (or 'pip install prompt-toolkit')."
            ) from exc

        with patch_stdout():
            return self._session.prompt(
                message=message,
                multiline=multiline,
                prompt_continuation=prompt_continuation,
                wrap_lines=wrap_lines,
                refresh_interval=1,
                lexer=self._lexer,
            )


def _split_lines_keepends(text: str) -> list[str]:
    # Python's splitlines(keepends=True) loses the last empty line marker; this is fine for our use.
    return text.splitlines(keepends=True)


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            starts.append(idx + 1)
    return starts


def _indent_len_for_line(line: str, indent: str) -> int:
    if line.startswith(indent):
        return len(indent)
    # Remove up to len(indent) spaces, or a single tab, whichever applies.
    if line.startswith("\t"):
        return 1
    n = 0
    for ch in line:
        if ch == " " and n < len(indent):
            n += 1
        else:
            break
    return n


def _get_selection_range(doc) -> Optional[Tuple[int, int, int]]:
    """
    Return (start, end, original_cursor_position) or None.
    Best-effort across prompt_toolkit internals.
    """
    sel = getattr(doc, "selection", None)
    if not sel:
        return None
    try:
        start, end = doc.selection_range()
    except Exception:
        orig = getattr(sel, "original_cursor_position", None)
        if not isinstance(orig, int):
            return None
        cur = int(getattr(doc, "cursor_position", 0))
        start, end = (orig, cur) if orig <= cur else (cur, orig)
    orig = getattr(sel, "original_cursor_position", None)
    if not isinstance(orig, int):
        orig = getattr(getattr(doc, "selection", None), "original_cursor_position", 0)
    return int(start), int(end), int(orig)


def _apply_linewise_transform(
    *,
    text: str,
    start: int,
    end: int,
    transform_line: callable,
) -> tuple[str, list[tuple[int, int]]]:
    """
    Apply transform_line(line)->(new_line, delta) on each line intersecting [start,end).
    Returns (new_text, per_line_deltas) where each delta item is (line_start_index, delta).
    """
    lines = _split_lines_keepends(text)
    starts = _line_starts(text)

    # Map selection positions to (row, col) to find which lines are affected.
    def pos_to_row(pos: int) -> int:
        # Linear scan is fine for REPL-sized buffers.
        row = 0
        for i, s in enumerate(starts):
            if s <= pos:
                row = i
            else:
                break
        return row

    start_row = pos_to_row(start)
    # Include the line where the end of the selection is located, even when it's
    # exactly at column 0 of a line (common editor behavior for indent/dedent).
    end_row = pos_to_row(max(start, end)) if end > start else start_row

    deltas: list[tuple[int, int]] = []
    out_lines: list[str] = []
    for i, line in enumerate(lines):
        if start_row <= i <= end_row:
            new_line, delta = transform_line(line)
            out_lines.append(new_line)
            if delta:
                deltas.append((starts[i], delta))
        else:
            out_lines.append(line)
    return "".join(out_lines), deltas


def _adjust_pos(pos: int, deltas: list[tuple[int, int]]) -> int:
    new_pos = pos
    for line_start, delta in deltas:
        if pos >= line_start:
            new_pos += delta
    return max(new_pos, 0)


def _build_lexer(highlighting: Literal["text", "python"]):
    if highlighting == "text":
        return None
    try:
        from prompt_toolkit.lexers import PygmentsLexer
        from pygments.lexers import PythonLexer
    except Exception as exc:
        raise RuntimeError(
            "Python syntax highlighting requires pygments. Install it (e.g. `pip install pygments`)."
        ) from exc
    return PygmentsLexer(PythonLexer)


def _ensure_ptk_shared() -> dict[str, Any]:
    """Initialize shared prompt_toolkit objects (key bindings, clipboard)."""
    global _PTK_SHARED
    if _PTK_SHARED is not None:
        return _PTK_SHARED

    # Some terminals scroll/jump when prompt_toolkit sends CPR (Cursor Position Request)
    # escape sequences. Disabling CPR makes the UX more stable and closer to the stock
    # CPython REPL in many terminal emulators.
    os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.document import Document
        from prompt_toolkit.selection import SelectionState
        from prompt_toolkit.clipboard import InMemoryClipboard
        from prompt_toolkit.history import InMemoryHistory, FileHistory
    except ImportError as exc:
        raise RuntimeError(
            "prompt_toolkit is required for terminal interactive shell mode. "
            "Install it via 'pip install \"pynteract[terminal]\"' (or 'pip install prompt-toolkit')."
        ) from exc

    try:
        from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard

        clipboard = PyperclipClipboard()
    except Exception:
        clipboard = InMemoryClipboard()

    kb = KeyBindings()
    indent = " " * 4

    def _next_tabstop(n: int, tabsize: int) -> int:
        if tabsize <= 0:
            return n
        return ((n // tabsize) + 1) * tabsize

    def _prev_tabstop(n: int, tabsize: int) -> int:
        if tabsize <= 0:
            return n
        return (n // tabsize) * tabsize

    def _spaces_to_next_boundary(prefix: str) -> int:
        tabsize = indent.count(" ")
        if not prefix or set(prefix) != {" "}:
            return tabsize
        n = len(prefix)
        return max(_next_tabstop(n, tabsize) - n, 1)

    def _spaces_to_prev_boundary(prefix: str) -> int:
        tabsize = indent.count(" ")
        if not prefix or set(prefix) != {" "}:
            return tabsize
        n = len(prefix)
        target = _prev_tabstop(n, tabsize)
        delete_n = n - target
        return tabsize if delete_n == 0 else delete_n

    def _indent_or_dedent_selection(buf, *, dedent: bool) -> None:
        doc = buf.document
        sel = _get_selection_range(doc)
        if sel is None:
            return
        start, end, orig = sel
        cursor = int(doc.cursor_position)

        def transform_line(line: str):
            if dedent:
                if line.startswith(" "):
                    lead = 0
                    for ch in line:
                        if ch == " ":
                            lead += 1
                        else:
                            break
                    remove_n = min(lead, _spaces_to_prev_boundary(" " * lead))
                    return (line[remove_n:], -remove_n)
                n = _indent_len_for_line(line, indent)
                return (line[n:], -n)

            if line.startswith(" "):
                lead = 0
                for ch in line:
                    if ch == " ":
                        lead += 1
                    else:
                        break
                add_n = _spaces_to_next_boundary(" " * lead)
                return (" " * add_n + line, add_n)

            return (indent + line, len(indent))

        new_text, deltas = _apply_linewise_transform(text=doc.text, start=start, end=end, transform_line=transform_line)

        new_start = _adjust_pos(start, deltas)
        new_end = _adjust_pos(end, deltas)
        new_cursor = _adjust_pos(cursor, deltas)
        new_orig = _adjust_pos(orig, deltas)

        buf.set_document(Document(new_text, cursor_position=new_cursor), bypass_readonly=True)
        buf.selection_state = SelectionState(original_cursor_position=new_orig)

        # Ensure cursor is within selection range.
        if not (new_start <= buf.cursor_position <= new_end):
            buf.cursor_position = new_end

    @kb.add(Keys.Tab)
    def _(event):
        buf = event.app.current_buffer
        if buf.selection_state:
            _indent_or_dedent_selection(buf, dedent=False)
            return
        doc = buf.document
        before = doc.current_line_before_cursor
        if before and set(before) == {" "}:
            buf.insert_text(" " * _spaces_to_next_boundary(before))
            return
        buf.insert_text(indent)

        @kb.add(Keys.BackTab)
        def _(event):
            buf = event.app.current_buffer
            if buf.selection_state:
                _indent_or_dedent_selection(buf, dedent=True)
                return

            doc = buf.document
            line = doc.current_line
            if line.startswith(" "):
                lead = 0
                for ch in line:
                    if ch == " ":
                        lead += 1
                    else:
                        break
                n = min(lead, _spaces_to_prev_boundary(" " * lead))
            else:
                n = _indent_len_for_line(line, indent)
            if n <= 0:
                return

            # Dedent the current line.
            starts = _line_starts(doc.text)
            row = doc.cursor_position_row
            line_start = starts[row]
            new_text = doc.text[:line_start] + doc.text[line_start + n :]
            new_cursor = max(doc.cursor_position - min(n, doc.cursor_position - line_start), line_start)
            buf.set_document(Document(new_text, cursor_position=new_cursor), bypass_readonly=True)

        @kb.add(Keys.ControlC)
        def _(event):
            buf = event.app.current_buffer
            if buf.selection_state:
                buf.copy_selection()
                buf.exit_selection()
                return
            raise KeyboardInterrupt

        @kb.add(Keys.ControlX)
        def _(event):
            buf = event.app.current_buffer
            if buf.selection_state:
                buf.cut_selection()
                buf.exit_selection()

        @kb.add(Keys.ControlV)
        def _(event):
            buf = event.app.current_buffer
            buf.paste_clipboard_data(event.app.clipboard.get_data())

        @kb.add(Keys.Enter)
        def _(event):
            buf = event.app.current_buffer
            multiline_attr = getattr(buf, "multiline", False)
            if callable(multiline_attr):
                is_multiline = bool(multiline_attr())
            else:
                # In prompt_toolkit, this may be a Filter; calling bool(filter) raises.
                try:
                    is_multiline = bool(multiline_attr)
                except Exception:
                    is_multiline = False

            if not is_multiline:
                buf.validate_and_handle()
                return

            doc = buf.document
            before = doc.current_line_before_cursor.rstrip()
            current_indent = ""
            for ch in doc.current_line:
                if ch in (" ", "\t"):
                    current_indent += ch
                else:
                    break
            extra = indent if before.endswith(":") else ""
            buf.insert_text("\n" + current_indent + extra)

        @kb.add(Keys.Backspace)
        def _(event):
            buf = event.app.current_buffer

            # If there is a selection, delete it (common editor behavior).
            if buf.selection_state:
                buf.cut_selection()
                buf.exit_selection()
                return

            doc = buf.document
            before = doc.current_line_before_cursor
            if before and set(before) == {" "}:
                # Only spaces before cursor on this line: snap back to the previous
                # indentation boundary (multiples of 4 spaces).
                n = indent.count(" ")
                remainder = len(before) % n
                delete_n = n if remainder == 0 else remainder
                buf.delete_before_cursor(count=delete_n)
                return

            buf.delete_before_cursor(count=1)

        def _exit_selection(event) -> None:
            buf = event.app.current_buffer
            if buf.selection_state:
                buf.exit_selection()

        # Arrow movement cancels selection (editor-like behavior).
        @kb.add(Keys.Left)
        def _(event):
            _exit_selection(event)
            event.current_buffer.cursor_left()

        @kb.add(Keys.Right)
        def _(event):
            _exit_selection(event)
            event.current_buffer.cursor_right()

        @kb.add(Keys.Up)
        def _(event):
            _exit_selection(event)
            event.current_buffer.cursor_up()

        @kb.add(Keys.Down)
        def _(event):
            _exit_selection(event)
            event.current_buffer.cursor_down()

        # History browsing (editor-like): Alt+Up / Alt+Down.
        @kb.add("escape", "up")
        def _(event):
            _exit_selection(event)
            event.current_buffer.history_backward(count=1)

        @kb.add("escape", "down")
        def _(event):
            _exit_selection(event)
            event.current_buffer.history_forward(count=1)

    _PTK_SHARED = {
        "PromptSession": PromptSession,
        "InMemoryHistory": InMemoryHistory,
        "FileHistory": FileHistory,
        "key_bindings": kb,
        "clipboard": clipboard,
    }
    return _PTK_SHARED


def _get_ptk_session(highlighting: Literal["text", "python"]) -> PtkSession:
    if highlighting not in ("text", "python"):
        raise ValueError("highlighting must be 'text' or 'python'")
    existing = _PTK_SESSIONS.get(highlighting)
    if existing is not None:
        return existing

    shared = _ensure_ptk_shared()
    history = None
    try:
        path = user_config.history_path(kind=highlighting)
        history = shared["FileHistory"](str(path))
    except Exception:
        history = shared["InMemoryHistory"]()
    session = PtkSession(
        highlighting=highlighting,
        key_bindings=shared["key_bindings"],
        clipboard=shared["clipboard"],
        history=history,
        prompt_session_cls=shared["PromptSession"],
    )
    _PTK_SESSIONS[highlighting] = session
    return session


def prompt(
    prompt: str,
    multiline: bool = True,
    prompt_continuation: str = "",
    wrap_lines: bool = False,
    *,
    highlighting: str = "text",
) -> str:
    """Read user input using prompt_toolkit with safe stdout patching.

    Args:
        highlighting: Either "text" (no highlighting, default) or "python".
    """
    if highlighting not in ("text", "python"):
        raise ValueError("highlighting must be 'text' or 'python'")
    session = _get_ptk_session(highlighting)
    return session.prompt(
        message=prompt,
        multiline=multiline,
        prompt_continuation=prompt_continuation,
        wrap_lines=wrap_lines,
    )
