from __future__ import annotations

import __future__
import ast
from dataclasses import dataclass


@dataclass
class FutureManager:
    """Track session-wide __future__ compiler flags for an interactive shell."""

    flags: int = 0
    allow_leading_docstring: bool = True
    _seen_non_future_stmt: bool = False
    _seen_docstring: bool = False

    def reset(self) -> None:
        self.flags = 0
        self._seen_non_future_stmt = False
        self._seen_docstring = False

    def begin_block(self) -> None:
        """Reset per-block state (flags are session-wide and persist)."""
        self._seen_non_future_stmt = False
        self._seen_docstring = False

    def process_node(self, node: ast.AST) -> None:
        """Validate/process a single node for __future__ semantics.

        Notebook/REPL rule: `from __future__ import ...` must be the first node of the input.
        By default, we emulate module semantics and permit exactly one leading string literal
        ("docstring position") before any future imports.

        If ``allow_leading_docstring`` is False, a leading string literal counts as a statement
        (it can be displayed as output) and therefore blocks future imports that follow.
        """

        if (
            self.allow_leading_docstring
            and not self._seen_non_future_stmt
            and not self._seen_docstring
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            self._seen_docstring = True
            return

        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if self._seen_non_future_stmt:
                raise SyntaxError("from __future__ imports must occur at the beginning of the input")
            for alias in node.names:
                feature = getattr(__future__, alias.name, None)
                flag = getattr(feature, "compiler_flag", 0) if feature is not None else 0
                if not flag:
                    raise SyntaxError(f"future feature {alias.name!r} is not defined")
                self.flags |= int(flag)
            return

        self._seen_non_future_stmt = True
