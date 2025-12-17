from .utils import short_id
from contextlib import contextmanager
import tokenize
import io
import re
from dataclasses import dataclass
from typing import Callable, Literal, Any

@dataclass(frozen=True, slots=True)
class Magic:
    func: Callable[[str], Any]
    mode: Literal["line", "cell", "both"] = "both"

    def __call__(self, text: str) -> Any:
        return self.func(text)
    
class MagicParser:

    def __init__(self):
        self._placeholders={}
        self._placeholder_stack=[]

    _INLINE_MAGIC_RE = re.compile(r"%(?P<name>[A-Za-z_][A-Za-z0-9_]*)")

    @staticmethod
    def _prev_non_space_char(line: str, idx: int) -> str | None:
        j = idx - 1
        while j >= 0 and line[j].isspace():
            j -= 1
        return line[j] if j >= 0 else None

    def _render_template(self, text: str, globals_dict: dict, locals_dict: dict) -> str:
        """IPython-style template expansion.

        - `{expr}` is evaluated as Python (via eval) and substituted with `str(value)`.
        - `{{` and `}}` escape literal braces.
        """
        out: list[str] = []
        i = 0
        n = len(text)
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
                value = eval(expr, globals_dict, locals_dict)
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
        raw = self._placeholders.get(key, "")
        return self._render_template(raw, globals_dict, locals_dict)

    def _find_inline_magic(self, line: str, line_no: int, ignore_map) -> tuple[int, str, str] | None:
        """Return (column, magic_name, rhs_text) for the first inline magic in `line`, if any."""
        allowed_prev = {"=", "(", "[", "{", ",", ":", ";"}
        for match in self._INLINE_MAGIC_RE.finditer(line):
            idx = match.start()
            if idx > 0 and line[idx - 1] == "%":
                # Avoid treating '%%' as an inline magic.
                continue
            if self._position_ignored(ignore_map, line_no, idx):
                continue

            prev = self._prev_non_space_char(line, idx)
            if prev is not None and prev not in allowed_prev:
                continue

            magic = match.group("name")
            rhs = line[match.end() :].lstrip()
            return idx, magic, rhs
        return None

    def _parse_system_cmd(self, code):
        """Parses and transforms system commands in the code.
        Args:
            code (str): The input code containing potential system commands.
        Returns:
            str: The transformed code with system commands replaced by function calls.
        """
        stripped_code=code.lstrip('\n')
        if stripped_code.startswith('!!'):
            command = stripped_code[2:].lstrip('\n')
            id= short_id()
            self._placeholders[id]=command
            return f"__shell__.run_system_cmd_capture(__shell__._magic_parser._render_placeholder('{id}', globals(), locals()))"

        lines = code.split('\n')
        ignore_map = self._build_ignore_map(code, lines)
        for i, line in enumerate(lines):
            stripped_line = line.lstrip()
            if stripped_line.startswith('!') and not stripped_line.startswith('!!'):
                column = len(line) - len(stripped_line)
                if self._position_ignored(ignore_map, i + 1, column):
                    continue
                indent = line[:column]
                command = stripped_line[1:].lstrip()
                id= short_id()
                self._placeholders[id]=command
                
                lines[i] = f"{indent}__shell__.run_system_cmd(__shell__._magic_parser._render_placeholder('{id}', globals(), locals()))"
        return '\n'.join(lines)

    def _parse_magics(self,code):
        """Parses and transforms magic commands in the code.
        Args:
            code (str): The input code containing potential magic commands.
        Returns:
            str: The transformed code with magic commands replaced by function calls.
        """
        stripped_code=code.lstrip('\n')
        if stripped_code.startswith('%%'):
            lines=stripped_code.split('\n')
            magic=lines[0][2:].strip()
            content='\n'.join(lines[1:])
            id= short_id()
            self._placeholders[id]=content
            code=f"__shell__._call_magic('{magic}', 'cell', __shell__._magic_parser._render_placeholder('{id}', globals(), locals()))"
        else:
            lines=code.split('\n')
            ignore_map = self._build_ignore_map(code, lines)
            for i,line in enumerate(lines):
                stripped=line.lstrip()
                if stripped.startswith("%"):
                    column=len(line)-len(stripped)
                    if self._position_ignored(ignore_map, i + 1, column):
                        continue
                    indent=line[:column]
                    parts=stripped.split(" ",1)
                    magic=parts[0][1:]
                    content=parts[1].strip() if len(parts)>1 else ""
                    id= short_id()
                    self._placeholders[id]=content
                    lines[i]=f"{indent}__shell__._call_magic('{magic}', 'line', __shell__._magic_parser._render_placeholder('{id}', globals(), locals()))"
                    continue

                inline = self._find_inline_magic(line, i + 1, ignore_map)
                if inline is None:
                    continue
                column, magic, content = inline
                id = short_id()
                self._placeholders[id] = content
                left = line[:column]
                replacement = f"__shell__._call_magic('{magic}', 'line', __shell__._magic_parser._render_placeholder('{id}', globals(), locals()))"
                lines[i] = f"{left}{replacement}"
            code='\n'.join(lines)
        return code

    @contextmanager
    def _placeholder_scope(self):
        """
        Provides an isolated placeholder store for a single shell run.

        A stack is used so nested shell runs (e.g. magics invoking __shell__.run)
        keep their placeholders separate.
        """
        previous = getattr(self, "_placeholders", {})
        self._placeholder_stack.append(previous)
        self._placeholders = {}
        try:
            yield self._placeholders
        finally:
            prior = self._placeholder_stack.pop() if self._placeholder_stack else {}
            self._placeholders = prior if prior is not None else {}

    @staticmethod
    def _position_ignored(ignore_map, line_no, column):
        """Checks if a given position is within an ignored range.
        Args:
            ignore_map (dict): The ignore map.
            line_no (int): The line number (1-based).
            column (int): The column number (0-based).
        Returns:
            bool: True if the position is ignored, False otherwise.
        """
        for start, end in ignore_map.get(line_no, ()):
            if start <= column < end:
                return True
        return False
    
    def _build_ignore_map(self, code, lines):
        """Builds a map of positions to ignore (inside strings or comments).
        Used to avoid parsing magics or system commands inside strings or comments.
        Args:
            code (str): The input code.
            lines (list): List of lines in the code.
        Returns:
            dict: A mapping of line numbers to lists of (start_col, end_col) tuples to ignore.
        """
        ignore = {}
        try:
            tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        except (tokenize.TokenError, IndentationError):
            return ignore

        for tok in tokens:
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                (start_line, start_col) = tok.start
                (end_line, end_col) = tok.end

                if start_line == end_line:
                    ignore.setdefault(start_line, []).append((start_col, end_col))
                else:
                    line_text = lines[start_line - 1] if start_line - 1 < len(lines) else ''
                    ignore.setdefault(start_line, []).append((start_col, len(line_text)))
                    for line in range(start_line + 1, end_line):
                        line_text = lines[line - 1] if line - 1 < len(lines) else ''
                        ignore.setdefault(line, []).append((0, len(line_text)))
                    ignore.setdefault(end_line, []).append((0, end_col))

        return ignore
