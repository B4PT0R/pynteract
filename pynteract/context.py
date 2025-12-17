from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

# Active capture streams for the current context (stdout, stderr).
CAPTURE_STREAMS: contextvars.ContextVar[tuple[Any, Any] | None] = contextvars.ContextVar(
    "pynteract_capture_streams",
    default=None,
)

@dataclass(frozen=True, slots=True)
class RunContext:
    name: str


# Current run context (routing name for hooks).
RUN_CONTEXT: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "pynteract_run_context",
    default=None,
)

# If True, suppress stdout/stderr hook calls for this run (still captures output).
SILENT_STDIO: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pynteract_silent_stdio",
    default=False,
)

# Whether code printed a prompt without a trailing newline (common with builtin
# input(prompt)), so stdin reads can start on a fresh line.
PENDING_STDIN_PROMPT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pynteract_pending_stdin_prompt",
    default=False,
)
