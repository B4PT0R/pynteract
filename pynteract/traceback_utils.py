from __future__ import annotations

import traceback
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


def _format_syntax_error_block(exc: SyntaxError) -> str:
    filename = getattr(exc, "filename", None) or "<unknown>"
    lineno = getattr(exc, "lineno", None) or 1
    text = getattr(exc, "text", None) or ""
    offset = getattr(exc, "offset", None)
    out = [f'  File "{filename}", line {lineno}\n']
    if text:
        stripped = text.rstrip("\n")
        out.append(f"    {stripped}\n")
        if isinstance(offset, int) and offset > 0:
            out.append("    " + (" " * (offset - 1)) + "^\n")
    out.extend(traceback.format_exception_only(type(exc), exc))
    return "".join(out)


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
    if filename == current_filename:
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
) -> tuple[str, dict[str, Any]]:
    """Build an enriched traceback string + structured frame info."""
    def format_one(exc: BaseException) -> tuple[str, dict[str, Any]]:
        te = traceback.TracebackException(type(exc), exc, exc.__traceback__)
        output = ["Traceback (most recent call last):\n"]
        enriched_frames: list[dict[str, Any]] = []

        stack = list(te.stack)
        start_index = 0
        for i, frame in enumerate(stack):
            if _is_user_frame(filename=frame.filename, current_filename=current_filename, history=history):
                start_index = i
                break

        for frame in stack[start_index:]:
            output.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n')
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
                    "filename": frame.filename,
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

    def format_chain(exc: BaseException) -> tuple[str, list[dict[str, Any]]]:
        parts: list[dict[str, Any]] = []

        if exc.__cause__ is not None:
            text, chain_parts = format_chain(exc.__cause__)
            parts.extend(chain_parts)
            text += "\nThe above exception was the direct cause of the following exception:\n\n"
            one_text, one_enriched = format_one(exc)
            parts.append(one_enriched)
            return text + one_text, parts

        if exc.__context__ is not None and not exc.__suppress_context__:
            text, chain_parts = format_chain(exc.__context__)
            parts.extend(chain_parts)
            text += "\nDuring handling of the above exception, another exception occurred:\n\n"
            one_text, one_enriched = format_one(exc)
            parts.append(one_enriched)
            return text + one_text, parts

        # Base case
        if isinstance(exc, SyntaxError) and exc.__traceback__ is None:
            # SyntaxError from parsing/compiling may have no traceback frames.
            text = "Traceback (most recent call last):\n" + _format_syntax_error_block(exc)
            parts.append({"frames": [], "skipped_frames": 0, "exception_lines": traceback.format_exception_only(type(exc), exc)})
            return text, parts

        one_text, one_enriched = format_one(exc)
        parts.append(one_enriched)
        return one_text, parts

    output_text, chain_enriched = format_chain(exc_value)
    enriched = {
        "exceptions": chain_enriched,
        # Back-compat: expose the outermost exception info at top-level.
        "frames": chain_enriched[-1]["frames"] if chain_enriched else [],
        "skipped_frames": chain_enriched[-1]["skipped_frames"] if chain_enriched else 0,
        "exception_lines": chain_enriched[-1]["exception_lines"] if chain_enriched else [],
    }
    return output_text, enriched
