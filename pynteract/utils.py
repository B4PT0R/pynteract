import hashlib
import secrets
import string
import sys
from typing import Any
from threading import Thread as ThreadBase

# Alphabet URL-safe pour les IDs lisibles (sans +/=)
_ID_ALPHABET = string.ascii_letters + string.digits


def content_hash(content: str) -> str:
    """Return a SHA256 hex digest for the provided content string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def short_id(length: int = 10) -> str:
    """Generate a cryptographically-safe, URL-safe random identifier.

    Uses ``secrets.choice`` over a 62-character alphabet (a-z A-Z 0-9).
    With ``length=10`` the collision probability for 1 million IDs is ~5e-9,
    negligible for placeholder keys.

    Args:
        length: Number of characters. Defaults to 10 (was 8 with random.choices).
    """
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def debug_print(*args: Any, sep: str = " ", end: str = "\n", flush: bool = False) -> None:
    """Write directly to the real stdout, bypassing patched streams."""
    sys.__stdout__.write(sep.join(map(str, args)) + end)
    if flush:
        sys.__stdout__.flush()


# ---------------------------------------------------------------------------
# Streamlit integration — opt-in via register_thread_context_hook()
# ---------------------------------------------------------------------------
# Keeping Streamlit awareness out of the hot path: the hook is None by default
# and only populated when the embedder explicitly calls
# ``register_thread_context_hook(fn)``.  This removes the unconditional
# try/import on every Thread() call and keeps pynteract free of Streamlit as
# a hard dependency.

_thread_context_hook: "Callable[[ThreadBase], None] | None" = None


def register_thread_context_hook(fn: "Callable[[ThreadBase], None]") -> None:
    """Register a callable that is invoked on every new Thread before it starts.

    Designed for Streamlit (or any other framework) that needs to propagate a
    per-request context to worker threads::

        from streamlit.runtime.scriptrunner import get_script_run_ctx, add_script_run_ctx
        register_thread_context_hook(lambda t: add_script_run_ctx(t, get_script_run_ctx()))

    Pass ``None`` to unregister.
    """
    global _thread_context_hook
    _thread_context_hook = fn


def Thread(*args, **kwargs) -> ThreadBase:
    """Drop-in replacement for ``threading.Thread`` with optional context injection.

    If a hook has been registered via :func:`register_thread_context_hook`,
    it is called with the new thread before returning.  Otherwise this is a
    transparent pass-through.
    """
    thread = ThreadBase(*args, **kwargs)
    if _thread_context_hook is not None:
        _thread_context_hook(thread)
    return thread
