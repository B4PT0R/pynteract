from __future__ import annotations

import traceback
import linecache
from typing import Any, Optional


def _is_user_frame(*, filename: str, current_filename: str, history: dict[str, Any]) -> bool:
    if filename == current_filename:
        return True
    if filename in history:
        return True
    # Heuristic: interactive inputs are typically synthetic filenames.
    if filename.startswith("<shell-input-"):
        return True
    return False


def render_snippet(
    *,
    filename: str,
    lineno: int,
    current_filename: str,
    current_code: Optional[str],
    history: dict[str, Any],
) -> str:
    """Render a small code snippet around `lineno` for a given filename.

    This mirrors the previous `Shell._render_snippet` behavior.
    """
    cached = linecache.getlines(filename)
    if cached:
        source = "".join(cached)
    elif filename == current_filename:
        source = current_code or ""
    elif filename in history:
        response = history.get(filename)
        source = getattr(response, "processed_input", "") or ""
    else:
        try:
            with open(filename, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            source = ""

    if not source:
        return ""

    lines = source.splitlines()
    target_index = lineno - 1
    if not (0 <= target_index < len(lines)):
        return ""

    start_index = max(target_index - 2, 0)
    end_index = min(target_index + 1, len(lines) - 1)
    snippet_lines: list[str] = []
    width = len(str(end_index + 1))
    for idx in range(start_index, end_index + 1):
        marker = "=> " if idx == target_index else "   "
        snippet_lines.append(f"{marker}{str(idx + 1).rjust(width)} | {lines[idx]}")
    return "\n".join(snippet_lines) + "\n"


def build_enriched_traceback(
    *,
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback,
    current_filename: str,
    current_code: Optional[str],
    history: dict[str, Any],
    cell_codes=None,
    source_names=None,
) -> tuple[str, dict[str, Any]]:
    """Render from the first cell frame, preserving every downstream call.

    Compiled code identity defines the boundary, not a package-path blacklist.
    Compilation errors have no executed cell frame; show their source location
    without the parser/compiler stack. Other errors without a cell frame (for
    example a failing pre-execution hook) retain their stack for diagnosis.
    The original exception traceback is never modified.
    """
    def format_one(exc: BaseException) -> tuple[str, dict[str, Any]]:
        tb = exc_traceback if exc is exc_value else exc.__traceback__
        te = traceback.TracebackException(type(exc), exc, tb)
        enriched_frames: list[dict[str, Any]] = []

        stack = list(te.stack)
        start_index = 0
        for i, (frame, _) in enumerate(traceback.walk_tb(tb)):
            is_cell = (frame.f_code in cell_codes if cell_codes is not None else
                       _is_user_frame(filename=frame.f_code.co_filename,
                                      current_filename=current_filename, history=history))
            if is_cell:
                start_index = i
                break
        else:
            if isinstance(exc, SyntaxError):
                start_index = len(stack)

        visible_stack = stack[start_index:]
        output = ["Traceback (most recent call last):\n"] if visible_stack else []
        for frame in visible_stack:
            display_filename = (source_names or {}).get(frame.filename, frame.filename)
            output.append(f'  File "{display_filename}", line {frame.lineno}, in {frame.name}\n')
            snippet = render_snippet(
                filename=frame.filename,
                lineno=frame.lineno,
                current_filename=current_filename,
                current_code=current_code,
                history=history,
            )
            if snippet:
                output.append(snippet)
            elif frame.line:
                output.append(f"    {frame.line.strip()}\n")

            enriched_frames.append(
                {
                    "filename": display_filename,
                    "lineno": frame.lineno,
                    "name": frame.name,
                    "line": frame.line,
                    "snippet": snippet,
                }
            )

        exception_lines = list(te.format_exception_only())
        output.extend(exception_lines)
        enriched = {
            "frames": enriched_frames,
            "skipped_frames": start_index,
            "exception_lines": exception_lines,
        }
        return "".join(output), enriched

    seen: set[int] = set()

    def format_chain(exc: BaseException) -> tuple[str, list[dict[str, Any]]]:
        # Walk causes iteratively: user code can create cycles or very long
        # chains. Never replace the original error with RecursionError.
        chain = []
        while id(exc) not in seen:
            seen.add(id(exc))
            cause = exc.__cause__
            context = exc.__context__ if not exc.__suppress_context__ else None
            previous = cause if cause is not None else context
            separator = ("The above exception was the direct cause of the following exception:"
                         if cause is not None else
                         "During handling of the above exception, another exception occurred:")
            chain.append((exc, separator))
            if previous is None:
                break
            exc = previous

        output = []
        parts = []
        for exc, separator in reversed(chain):
            if output:
                output.append("\n" + separator + "\n\n")
            text, enriched = format_one(exc)
            output.append(text)
            if isinstance(exc, BaseExceptionGroup):
                children = []
                for i, child in enumerate(exc.exceptions, 1):
                    child_text, child_parts = format_chain(child)
                    output.append(f"  +-- sub-exception {i} --\n")
                    output.append("".join("  | " + line for line in child_text.splitlines(True))
                                  or "  | [exception already shown]\n")
                    children.append({"exceptions": child_parts})
                enriched["children"] = children
            parts.append(enriched)
        return "".join(output), parts

    output_text, chain_enriched = format_chain(exc_value)
    enriched = {
        "exceptions": chain_enriched,
        # Back-compat: expose the outermost exception info at top-level.
        "frames": chain_enriched[-1]["frames"] if chain_enriched else [],
        "skipped_frames": chain_enriched[-1]["skipped_frames"] if chain_enriched else 0,
        "exception_lines": chain_enriched[-1]["exception_lines"] if chain_enriched else [],
    }
    return output_text, enriched
