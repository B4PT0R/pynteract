"""Shell
=================

A lightweight execution engine that brings IPython-style ergonomics to embedded
Python environments while staying dependency-light.

Highlights
----------
- Node-by-node execution powered by ASTTokens so hooks can inspect, transform
  or log each AST node before and after it runs.
- Streaming IO capture via `Collector`, custom `Stream` wrappers and a
  prompt_toolkit-aware `StdinProxy`, all pluggable through hooks.
- Token-aware support for single-line (`%`) and cell (`%%`) magics plus system
  commands (`!`, `!!`) that respect indentation and ignore occurrences inside
  strings or comments.
- Comprehensive hook matrix (pre/post run, code blocks, namespace diffing,
  display, exception, stdin/stdout/stderr) enabling fine-grained customization
  for agents, REPLs or UIs.
- Rich execution results encapsulated in `ShellResponse`, exposing captured
  stdout/stderr, last expression value, namespaces before/after, and any
  exception information.

Hook Matrix
-----------
The `Shell` class exposes hooks to customize every stage of execution. Each hook
receives rich context so embedders can log, transform or short-circuit behaviour:

- `input_hook(code)` runs before parsing the source string (logging, metrics).
- `pre_run_hook(code)` lets you rewrite code before it is tokenized/executed.
- `code_block_hook(code_block)` fires for every AST block (useful for tracing).
- `pre_execute_hook(node, source)` can mutate AST nodes prior to compilation.
- `post_execute_hook(node, result)` observes successfully executed nodes.
- `display_hook(result, kwargs)` overrides how expression values are rendered. kwargs is a dict of any extra args passed to `Shell.display()`.
- `stdout_hook(data, buffer)` / `stderr_hook(data, buffer)` redirect output streams to custom handlers.
- `stdin_hook()` redirect stdin reads to a custom handler (web, CLI, agents).
- `exception_hook(exc)` is invoked once a run finishes with an error.
- `namespace_change_hook(old, new, locals)` observes namespace changes.
- `post_run_hook(response)` sees the aggregate `ShellResponse` for logging or
  telemetry.

Hooks can be combined; each is optional and falls back to a sensible default when
not provided.


Quick Start
-----------
    shell = Shell()
    shell.register_magic(lambda text: text.upper(), name="caps")
    shell.run('''%caps hello
!pwd
value = 41 + 1''')

    response = shell.run("value")
    print(response.result)  # 42

This module is designed to be embedded in CLI tools, notebooks, web consoles or
agents that need controllable Python execution without the dependency weight of
IPython.
"""

from __future__ import annotations

import sys
import builtins
import ast
import asyncio
import inspect
import contextvars
import threading
from contextlib import contextmanager
from types import SimpleNamespace, CodeType
from weakref import WeakSet
from dataclasses import replace
import os

from collections import OrderedDict
from asttokens import ASTTokens
import io

from typing import Any, Callable, Optional, Union, Literal
from .utils import short_id, call_sync_hook
from .processes import run_streamed
from .sources import SourceCache
from .scripts import read_script
from .ptk import prompt
from .collector import Collector
from .magics import MagicParser, Magic
from . import namespace_utils
from . import traceback_utils
from . import future_utils
from . import user_config
from .context import RUN_CONTEXT, RunContext, SILENT_STDIO, PENDING_STDIN_PROMPT


_cwd_lock = threading.Lock()
_active_executions = {}


def _execution_owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@contextmanager
def _cwd_execution_scope(exclusive):
    # cwd is process-global. Refuse unsafe overlap rather than blocking an event
    # loop or claiming os.chdir can be isolated. Inline nested runs remain valid.
    owner = _execution_owner()
    with _cwd_lock:
        others = [value for key, value in _active_executions.items() if key != owner]
        if others and (exclusive or any(value[1] for value in others)):
            raise RuntimeError(
                "Concurrent execution requires use_internal_cwd=False and no cwd argument"
            )
        counts = _active_executions.setdefault(owner, [0, 0])
        counts[0] += 1
        counts[1] += int(exclusive)
    try:
        yield
    finally:
        with _cwd_lock:
            counts[0] -= 1
            counts[1] -= int(exclusive)
            if not counts[0]:
                del _active_executions[owner]


class ExecutionInterrupted(BaseException):
    """An explicit host interruption; unwinds user code without closing its Shell.

    Unlike KeyboardInterrupt this does not trigger asyncio's runner shutdown.
    Like KeyboardInterrupt it is deliberately not caught by ``except Exception``.
    """


class ShellResponse:
    """
    Represents the results of code execution, encapsulating various aspects of the execution.

    Attributes:
        input (str): The original input code.
        processed_input (str): The code after preprocessing.
        stdout (str): Captured standard output.
        output_stats (dict): Per-stream written_chars and truncated flag.
        stderr (str): Captured standard error.
        result (Any): The result of the last executed expression.
        exception (Exception): Any exception that occurred during execution.
    """
    def __init__(
        self,
        input: Optional[str] = None,
        processed_input: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        result: Any = None,
        exception: Optional[Exception] = None,
        output_stats: Optional[dict] = None,
    ) -> None:
        self.input = input
        self.processed_input = processed_input
        self.stdout = stdout
        self.stderr = stderr
        self.result = result
        self.exception = exception
        self.output_stats = output_stats or {}

    @staticmethod
    def _short_repr(value: Any, *, limit: int = 100) -> str:
        text = repr(value)
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def __repr__(self) -> str:
        parts = [self.__class__.__name__ + "("]
        fields = []
        if self.result is not None:
            fields.append(f"result={self._short_repr(self.result)}")
        if self.exception is not None:
            fields.append(f"exception={self._short_repr(self.exception)}")
        if self.stdout:
            fields.append(f"stdout_len={len(self.stdout)}")
        if self.stderr:
            fields.append(f"stderr_len={len(self.stderr)}")
        if self.processed_input:
            fields.append(f"processed_input_len={len(self.processed_input)}")
        if not fields:
            fields.append("empty")
        parts.append(", ".join(fields))
        parts.append(")")
        return "".join(parts)

    def __str__(self) -> str:
        return self.__repr__()

class Shell:
    """
    Executes Python code within a managed environment and captures output and exceptions.

    This class provides a flexible and extensible Python code execution environment.
    It allows for fine-grained control over code execution, input/output handling,
    and namespace management through various hooks and customization options.

    Parameters:
        namespace (dict): The global namespace for code execution.
        display_mode (str): Controls when results are displayed ('all', 'last', or 'none').
        history_size (int): Maximum number of past executions to cache.
        capture_limit (int | None): Optional per-stream character limit per run.
            Keeps a prefix plus a visible marker; subsequent output is discarded
            from capture and hooks, but counted in response.output_stats.
        hook_buffer (bool): Pass cumulative text to stream hooks (default True).
            False passes an empty second argument, avoiding cumulative copies.
        silent (bool): Default silent mode for runs. If True, suppress stdout/stderr hook
            calls by default while still capturing output. Per-call ``run(..., silent=...)``
            overrides this default.
        use_internal_cwd (bool): False by default: share the ambient process cwd
            without implicit chdir. If True, temporarily apply a saved shell cwd
            and restore the caller cwd; this mode requires exclusive execution.

        Hooks:
            Hooks are stored in ``self.hooks`` (a dict) and are passed to ``Shell(...)`` as keyword
            arguments ending in ``_hook`` (e.g. ``stdout_hook=...``). Unknown hook keys are accepted
            as long as they follow the ``*_hook`` naming convention.

    Public Attributes:
        namespace (dict): The global namespace for code execution.
        display_mode (str): Controls when results are displayed ('all', 'last', or 'none').
        magics (dict): Registered magic commands.
        history (OrderedDict): Cache of past executions.
        history_size (int): Maximum number of past executions to cache.
        current_code (str): The current code being executed.
        last_result (Any): The result of the last execution.
        hooks (dict): Mapping of hook-name to callable (or None).

    Public Methods:
        run(code, globals, locals): Execute the given code in the shell environment.
        interact(): Starts an interactive shell session with multiline input support.
        ensure_builtins(): Ensures built-in functions and classes are available in the namespace.
        set_namespace(namespace): Dynamically sets the namespace reference to a chosen dict.
        reset_namespace(): Clears the namespace, retaining only built-in functions and classes.
        update_namespace(*args, **kwargs): Dynamically updates the namespace with provided variables or functions.
        display(obj): Default method to display an object.

    Expected hooks signatures:
        Hooks receive only their payload. Use ``pynteract.get_run_ctx()`` when
        routing is needed: its ``name`` matches the current run filename.
        Outside execution it returns ``None``.

        input_hook(code)
        pre_run_hook(code) -> processed_code
        code_block_hook(code_block)
        pre_execute_hook(node, source) -> node
        post_execute_hook(node, result)
        display_hook(result, kwargs)
        stdout_hook(data, buffer)
        stderr_hook(data, buffer)
        stdin_hook() -> str or None
        exception_hook(exc)
        namespace_change_hook(old_globals, new_globals, locals)
        post_run_hook(response) -> response
        add_script_run_ctx_hook(thread, script_ctx)
        get_script_run_ctx_hook() -> script_ctx
        
    """

    def __init__(
        self,
        # Configuration
        namespace: Optional[dict[str, Any]] = None,
        module_name: str | None = None,
        ensure_cwd_on_syspath: bool = True,
        display_mode: Literal['all', 'last', 'none'] = 'last',
        history_size: int = 200,
        silent: bool = False,
        use_internal_cwd: bool = False,
        capture_limit: int | None = None,
        hook_buffer: bool = True,
        # Hooks (passed by name via **hooks; see docs above for supported keys)
        **hooks: Any,
    ) -> None:

        # Hooks registry (kept in a dict to make extension easy and to support
        # dynamic hook replacement during a run).
        invalid = [name for name in hooks.keys() if not name.endswith("_hook")]
        if invalid:
            invalid_text = ", ".join(sorted(invalid))
            raise TypeError(f"Unknown non-hook kwargs: {invalid_text}")
        if capture_limit is not None and (type(capture_limit) is not int or capture_limit < 0):
            raise ValueError("capture_limit must be a nonnegative integer or None")
        self.capture_limit = capture_limit
        self.hook_buffer = bool(hook_buffer)
        self.hooks: dict[str, Any] = dict(hooks)
        self._execution_lock = threading.RLock()
        self._execution_owner = None
        self._execution_depth = 0
        self._closed = False

        # Execution environment:
        # - Use a module-backed namespace by default so classes/functions get a sane __module__,
        #   typing.get_type_hints can resolve forward refs, and behavior matches Python more closely.
        # - Ensure '' is on sys.path (interactive-Python behavior: imports resolve from CWD).
        self._ensure_cwd_on_syspath = bool(ensure_cwd_on_syspath)

        # Initialize filename early: namespace syncing needs it.
        self._current_filename = "<shell-input-0>"

        # Namespace setup (do not call ensure_builtins() yet: it depends on self.magics/display).
        self._ns = namespace_utils.NamespaceManager(
            module_name=module_name,
            filename=self._current_filename,
            ensure_cwd_on_syspath=self._ensure_cwd_on_syspath,
            namespace=namespace,
        )

        # Session-wide compiler flags from __future__ imports (interactive behavior).
        # Important: we always compile with dont_inherit=True so we never leak the host module's
        # future imports (e.g. from __future__ import annotations) into user code accidentally.
        self._futures = future_utils.FutureManager()
        # Identify cell frames without filenames or retaining dead compiled cells.
        self._cell_codes = WeakSet()
        self._sources = SourceCache()

        self.display_mode = display_mode
        self.silent = bool(silent)
        self.use_internal_cwd = bool(use_internal_cwd)
        self.magics= {}
        self._magic_parser=MagicParser()
        self.last_result = None
        self._current_code=None
        # _current_filename already initialized above
        self.history_size=max(history_size,1)
        self.history=OrderedDict()
        self._input_counter=0
        self.session=None
        self._cwd = os.path.abspath(os.getcwd())
        self.ensure_builtins()
        self._startup_ran = False
        self._startup_announced = False
        self._startup_has_source = False
        self._startup_failed = False
        self._startup_error_message = ""

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Release owned module registrations without clearing user data.

        Idempotent, but not allowed during a cell (including from its hooks).
        A closed shell cannot execute or structurally mutate its namespace.
        """
        with self._execution_lock:
            if self._execution_depth:
                raise RuntimeError("Cannot close a Shell while it is executing")
            if not self._closed:
                self._ns.close()
                self._sources.close()
                self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _check_open(self):
        if self._closed:
            raise RuntimeError("This Shell is closed")

    def restart_session(self, *, rerun_startup: bool = True, announce: bool = True) -> int:
        """Reset the user namespace and (optionally) rerun the startup script.

        Intended for interactive use via `__shell__.restart_session()`.

        Args:
            rerun_startup: If True, rerun `~/.pynteract/startup.py` after resetting.
            announce: If True, print the "Running startup..." / "Ready!" messages when rerunning startup.

        Returns:
            int: 0 on success, 1 if startup fails.
        """
        preserved: dict[str, Any] = {}
        exit_fn = self.namespace.get("exit")
        if callable(exit_fn):
            preserved["exit"] = exit_fn

        self.reset_namespace()

        if preserved:
            self.update_namespace(**preserved)

        if not rerun_startup:
            return 0

        self._reset_startup_state()
        return self._run_startup(announce=announce)

    def enable_stdio_proxy(self) -> None:
        """Install a routing proxy on sys.stdout/sys.stderr for thread-aware capture."""
        from .streams import install_stdio_proxy

        install_stdio_proxy()

    def capture_context(self, *, name: str | None = None) -> contextvars.Context:
        """Return a contextvars Context suitable for running work in another thread.

        - `capture_context()` captures the current run context (including `ctx.name`)
          and any active capture streams.
        - `capture_context(name=...)` returns a context that is identical, but with the
          run context overridden so hooks see `ctx.name == name`.

        Use `ctx.run(fn, *args)` as a thread target to keep output routed to the
        originating run/cell (or to the overridden `name`).
        """
        ctx = contextvars.copy_context()
        if name is not None:
            ctx.run(RUN_CONTEXT.set, RunContext(name=str(name)))
        return ctx

    @property
    def namespace(self) -> dict[str, Any]:
        """Module-backed execution namespace (canonical)."""
        return self._ns.namespace

    @property
    def cwd(self) -> str:
        """Current shell working directory.

        In internal-cwd mode this is shell-local state. In ambient-cwd mode it
        reflects the hosting process cwd directly.
        """
        if self.use_internal_cwd:
            return self._cwd
        return os.path.abspath(os.getcwd())

    @cwd.setter
    def cwd(self, value: str | os.PathLike[str]) -> None:
        normalized = os.path.abspath(os.path.expanduser(os.path.expandvars(os.fspath(value))))
        if self.use_internal_cwd:
            self._cwd = normalized
        else:
            os.chdir(normalized)

    @property
    def current_code(self):
        """Returns the current code being executed (readonly)."""
        return self._current_code
    
    def _build_enriched_traceback(self, exc_type, exc_value, exc_traceback):
        """Builds an enriched traceback with code snippets for each frame.
        Args:
            exc_type: The type of the exception.
            exc_value: The exception instance.
            exc_traceback: The traceback object.
        Returns:
            tuple: A tuple containing the formatted traceback string and a dictionary with enriched frame information.
        """
        return traceback_utils.build_enriched_traceback(
            exc_type=exc_type,
            exc_value=exc_value,
            exc_traceback=exc_traceback,
            current_filename=self._current_filename,
            current_code=self._current_code,
            history=self.history,
            cell_codes=self._cell_codes,
            source_names=self._sources.names,
        )


    def register_magic(self, func=None, *, name=None, mode: Literal["line", "cell", "both"] = "both"):
        """Registers a magic function.
        Args:
            func (callable): The function to register as a magic.
            name (str, optional): The name of the magic. If None, uses func.__name__.
            mode (str): One of 'line', 'cell', 'both' to control which syntax is allowed.
        Returns:
            callable: The registered magic function.
        """
        if func is None:
            return lambda f: self.register_magic(f, name=name, mode=mode)
        if mode not in ("line", "cell", "both"):
            raise ValueError("mode must be one of: 'line', 'cell', 'both'")
        name = name or func.__name__
        self.magics[name] = Magic(func=func, mode=mode)
        return func

    def _call_magic(self, name: str, kind: Literal["line", "cell"], text: str) -> Any:
        magic = self.magics.get(name)
        if magic is None:
            raise KeyError(f"Unknown magic: {name}")

        magic_mode = getattr(magic, "mode", "both")
        if kind == "line" and magic_mode not in ("line", "both"):
            raise ValueError(f"Magic %{name} does not support line mode")
        if kind == "cell" and magic_mode not in ("cell", "both"):
            raise ValueError(f"Magic %%{name} does not support cell mode")

        return magic(text)

    def _normalize_cwd(self, cwd: str | os.PathLike[str] | None) -> str:
        if cwd is None:
            cwd = self.cwd
        return os.path.abspath(os.path.expanduser(os.path.expandvars(os.fspath(cwd))))

    def _finalize_cwd(self, fallback: str) -> None:
        try:
            self.cwd = os.path.abspath(os.getcwd())
        except OSError:
            self.cwd = fallback

    def _run_process(self, command, *, shell=False, stdout=None):
        prepare_thread = None
        add_hook = self.hooks.get("add_script_run_ctx_hook")
        if add_hook:
            get_hook = self.hooks.get("get_script_run_ctx_hook")
            ctx = call_sync_hook(get_hook) if get_hook else None
            prepare_thread = lambda thread: call_sync_hook(add_hook, thread, ctx)
        return run_streamed(command, shell=shell, stdout=stdout, prepare_thread=prepare_thread)

    def run_system_cmd(self, command):
        """Run a blocking system command, streaming IO and propagating delivery errors."""
        return self._run_process(command, shell=True)

    def run_system_cmd_capture(self, command: str) -> str:
        """Return stdout (``!!``), streaming stderr; failures still clean up the child."""
        output = io.StringIO()
        self._run_process(command, shell=True, stdout=output)
        return output.getvalue()


    def _compile_node(self, node, source, *, allow_await=False):
        hook = self.hooks.get("pre_execute_hook")
        if hook:
            node = call_sync_hook(hook, node, source)
            if not isinstance(node, ast.AST):
                raise TypeError("pre_execute_hook must return an AST node")
        expression = isinstance(node, ast.Expr)
        if not expression:
            self.last_result = None
        tree = ast.Expression(node.value) if expression else ast.Module([node], type_ignores=[])
        compiled = compile(
            tree, filename=source._pynteract_filename,
            mode="eval" if expression else "exec",
            flags=self._futures.flags | (ast.PyCF_ALLOW_TOP_LEVEL_AWAIT if allow_await else 0),
            dont_inherit=True,
        )
        self._register_cell_code(compiled)
        return node, compiled, expression

    def _register_cell_code(self, code):
        self._cell_codes.add(code)
        for constant in code.co_consts:
            if isinstance(constant, CodeType):
                self._register_cell_code(constant)

    def _finish_node(self, node, result, expression, suppress_result, is_last_node):
        self.last_result = result if expression else None
        if expression and not suppress_result:
            if self.display_mode == 'all' or (self.display_mode == 'last' and is_last_node):
                self.display(result)
        hook = self.hooks.get("post_execute_hook")
        if hook:
            call_sync_hook(hook, node, self.last_result)

    def _select_exec_context(self, globals_dict, locals_dict):
        """Return (exec_globals, exec_locals, is_session_namespace)."""
        if globals_dict is None:
            exec_globals = self.namespace
            is_session_namespace = True
        else:
            if not isinstance(globals_dict, dict):
                raise TypeError("Shell.run(..., globals=...) must be a dict")
            exec_globals = globals_dict
            is_session_namespace = False

        exec_locals = exec_globals if locals_dict is None else locals_dict
        return exec_globals, exec_locals, is_session_namespace

    def _next_filename(self, filename: str | None) -> str:
        self._input_counter += 1
        if filename is not None:
            self._current_filename = filename
        else:
            self._current_filename = f"<shell-input-{self._input_counter}>"
        return self._current_filename

    def _iter_nodes(self, processed_code: str, *, filename: str, allow_await=False):
        source = ASTTokens(processed_code, parse=True, filename=filename)
        nodes = source.tree.body
        # Validate the entire cell before any node can produce side effects.
        # Future flags are committed only after successful validation.
        futures = replace(self._futures)
        futures.begin_block()
        for node in nodes:
            futures.process_node(node)
        compile(source.tree, filename, "exec", dont_inherit=True,
                flags=futures.flags | (ast.PyCF_ALLOW_TOP_LEVEL_AWAIT if allow_await else 0))
        self._futures = futures
        source._pynteract_filename = self._sources.register(filename, processed_code)
        for i, node in enumerate(nodes):

            next_token = source.next_token(node.last_token)
            suppress_result = bool(next_token and next_token.string == ';')
            is_last_node = (i == len(nodes) - 1)

            startpos = node.first_token.startpos
            endpos = next_token.endpos if suppress_result else node.last_token.endpos
            code_block = source.text[startpos:endpos]

            code_block_hook = self.hooks.get("code_block_hook")
            if code_block_hook:
                call_sync_hook(code_block_hook, code_block)

            yield node, source, suppress_result, is_last_node

    @contextmanager
    def _namespace_mutation_scope(self):
        with self._execution_lock:
            self._check_open()
            if self._execution_depth and self._execution_owner != _execution_owner():
                raise RuntimeError("Cannot mutate the namespace while this Shell is executing")
            yield

    def interrupt(self):
        """Interrupt the active cell, returning False if idle.

        For synchronous execution call from the owning thread, typically in a
        host-installed signal handler. An async run can also be interrupted from
        another task/thread: its task is cancelled and arun raises
        ExecutionInterrupted after cleanup. No process-wide signal handlers or
        unsafe cross-thread exception injection are installed by the library.
        Native calls may delay delivery; user code can catch/ignore interruption.
        Namespace mutations already made are retained, not rolled back.
        """
        with self._execution_lock:
            if not self._execution_depth:
                return False
            owner_thread, task = self._execution_owner
            state = self._interrupt_state
            if owner_thread == threading.get_ident() and task is _execution_owner()[1]:
                raise ExecutionInterrupted()
            if task is None:
                raise RuntimeError("Interrupt synchronous execution from its owning thread")
            if not state.requested:
                state.requested = True
                task.get_loop().call_soon_threadsafe(self._cancel_execution, state, task)
        return True

    @staticmethod
    def _cancel_execution(state, task):
        if state.active and not task.done():
            state.cancel_sent = task.cancel()

    @contextmanager
    def _execution_scope(self, cwd):
        owner = _execution_owner()
        with self._execution_lock:
            self._check_open()
            if self._execution_depth and self._execution_owner != owner:
                raise RuntimeError("This Shell is already executing a cell")
            self._execution_owner = owner
            if not self._execution_depth:
                self._interrupt_state = SimpleNamespace(active=True, requested=False, cancel_sent=False)
            self._execution_depth += 1
        state = self._interrupt_state
        nested = self._execution_depth > 1
        previous = self._current_filename, self._current_code, self.last_result
        try:
            with _cwd_execution_scope(self.use_internal_cwd or cwd is not None):
                yield
        except asyncio.CancelledError:
            if not nested and state.cancel_sent:
                # Remove only the cancellation we issued, not an external one.
                remaining = owner[1].uncancel()
                state.cancel_sent = False
                if not remaining:
                    raise ExecutionInterrupted() from None
            raise
        finally:
            if not nested and state.cancel_sent:
                owner[1].uncancel()  # User code may have suppressed our cancellation.
            if nested:
                self._current_filename, self._current_code, self.last_result = previous
                self._ns.set_current_filename(self._current_filename)
            with self._execution_lock:
                self._execution_depth -= 1
                if not self._execution_depth:
                    state.active = False
                    self._execution_owner = None

    @contextmanager
    def _run_scope(self, code, globals, locals, silent, filename, cwd):
        previous_process_cwd = os.path.abspath(os.getcwd())
        run_cwd = self._normalize_cwd(cwd)
        if self.use_internal_cwd or cwd is not None:
            os.chdir(run_cwd)
        filename = self._next_filename(filename)
        token_ctx = RUN_CONTEXT.set(RunContext(name=filename))
        run_silent = self.silent if silent is None else bool(silent)
        token_silent = SILENT_STDIO.set(run_silent)
        token_prompt = PENDING_STDIN_PROMPT.set(False)
        original_code = code
        try:
            input_hook = self.hooks.get("input_hook")
            if input_hook:
                call_sync_hook(input_hook, code)

            exec_globals, exec_locals, is_session_namespace = self._select_exec_context(globals, locals)

            with self._magic_parser._placeholder_scope():
                code = self._magic_parser._parse_system_cmd(code)
                code = self._magic_parser._parse_magics(code)

                pre_run_hook = self.hooks.get("pre_run_hook")
                if pre_run_hook:
                    processed_code = call_sync_hook(pre_run_hook, code)
                else:
                    processed_code = code

                if is_session_namespace:
                    self._ns.set_current_filename(self._current_filename)
                self._ns.bind_namespace(
                    exec_globals,
                    filename=filename,
                    module_name=self._ns.module_name if is_session_namespace else None,
                )

                self._current_code = processed_code
                old_globals = dict(exec_globals)
                self.last_result = None

                state = SimpleNamespace(
                    code=processed_code, filename=filename,
                    globals=exec_globals, locals=exec_locals, collector=Collector(self),
                    response=None,
                )
                yield state
                collector = state.collector

                namespace_change_hook = self.hooks.get("namespace_change_hook")
                if namespace_change_hook:
                    call_sync_hook(namespace_change_hook, old_globals, exec_globals, exec_locals)

                if "__" in exec_globals:
                    exec_globals["___"] = exec_globals["__"]
                if "_" in exec_globals:
                    exec_globals["__"] = exec_globals["_"]
                exec_globals["_"] = self.last_result

                response = ShellResponse(
                    input=original_code,
                    processed_input=processed_code,
                    output_stats={name: {"written_chars": stream.written_chars,
                                         "truncated": stream._truncated}
                                  for name, stream in (("stdout", collector.stdout_stream),
                                                       ("stderr", collector.stderr_stream))},
                    stdout=collector.get_stdout(),
                    stderr=collector.get_stderr(),
                    result=self.last_result,
                    exception=collector.exception,
                )

                post_run_hook = self.hooks.get("post_run_hook")
                if post_run_hook:
                    response = call_sync_hook(post_run_hook, response)

                self.add_to_history(filename, response)
                state.response = response
        finally:
            try:
                if self.use_internal_cwd:
                    self._finalize_cwd(run_cwd)
                    os.chdir(previous_process_cwd)
            finally:
                PENDING_STDIN_PROMPT.reset(token_prompt)
                SILENT_STDIO.reset(token_silent)
                RUN_CONTEXT.reset(token_ctx)


    def run(self, code, globals=None, locals=None, silent=None, filename=None, cwd=None):
        """
        Execute the given code in the shell environment.

        Args:
            code (str): The Python code to execute.
            globals (dict, optional): Global namespace to use. If None, uses self.namespace.
            locals (dict, optional): Local namespace to use. If None, globals will be used.
            silent (bool, optional): If provided, overrides the shell's default silent mode for this run.
            filename (str, optional): Custom filename used in tracebacks/history (useful for notebooks/cells).
            cwd (str or os.PathLike, optional): Working directory for this run. In
                internal-cwd mode, the final cwd after user code runs is saved back to
                ``self.cwd`` and the caller process cwd is restored. In ambient-cwd
                mode, the process cwd is left wherever the run/user code ended.

        Returns:
            ShellResponse: An object containing the results of the execution.
        """
        with self._execution_scope(cwd), self._run_scope(code, globals, locals, silent, filename, cwd) as state:
            with state.collector:
                for node, source, suppressed, last in self._iter_nodes(state.code, filename=state.filename):
                    node, compiled, expression = self._compile_node(node, source)
                    result = eval(compiled, state.globals, state.locals)
                    self._finish_node(node, result, expression, suppressed, last)
        return state.response

    async def arun(self, code, globals=None, locals=None, silent=None, filename=None, cwd=None):
        """Execute in the caller's task, supporting top-level await/async for/async with.

        Synchronous code and hooks remain synchronous: blocking work blocks the
        event loop. Use ``await asyncio.to_thread(shell.run, code)`` explicitly
        for blocking cells. Ordinary expression results are not auto-awaited.
        Async stdout/stderr hooks are drained before return; other hooks are sync.
        Concurrent cells require separate shells with ``use_internal_cwd=False``
        and no explicit ``cwd``. Process cwd mutations are never task-local.
        """
        with self._execution_scope(cwd), self._run_scope(code, globals, locals, silent, filename, cwd) as state:
            async with state.collector:
                for node, source, suppressed, last in self._iter_nodes(state.code, filename=state.filename, allow_await=True):
                    node, compiled, expression = self._compile_node(node, source, allow_await=True)
                    result = eval(compiled, state.globals, state.locals)
                    if compiled.co_flags & inspect.CO_COROUTINE:
                        result = await result
                    self._finish_node(node, result, expression, suppressed, last)
        return state.response

    def add_to_history(self, filename, response):
        """Add a ``ShellResponse`` to the cache, enforcing the size limit.

        Args:
            filename: Synthetic filename (e.g. ``<shell-input-1>``) used as the
                history key.
            response: The response object to add to history.
        """
        self.history[filename]=response
        while len(self.history)>self.history_size:
            self.history.popitem(last=False)
    
    def display(self, obj, **kwargs):
        """
        Default method to display an object.

        Args:
            obj: The object to be displayed.
            **kwargs: Optional arguments to pass to the display hook
                (e.g., backend='json' for custom display backends).

        This method attempts to use ``self.hooks['display_hook']`` if provided,
        passing any additional kwargs to it. Falls back to printing
        the repr of the object if no hook is configured.
        """
        if obj is not None:
            display_hook = self.hooks.get("display_hook")
            if display_hook:
                call_sync_hook(display_hook, obj, kwargs)
            else:
                print(repr(obj))


    def interact(self) -> int:
        """
        Starts an interactive shell session with multiline input support.
        """
        print("Welcome to Pynteract's Interactive Shell.")
        print(f"Python {sys.version.split()[0]}")

        startup_exit = self._run_startup(announce=True)
        if startup_exit != 0:
            return startup_exit

        print("Use Alt+Enter to submit your input.")
        print("Type 'exit()' to exit the shell.")

        loop = True

        def custom_exit():
            nonlocal loop
            loop = False

        self.update_namespace(exit=custom_exit)
        
        while loop:
            try:
                code = prompt(">>> ", prompt_continuation="... ", highlighting="python")
            except KeyboardInterrupt:
                print("\nKeyboardInterrupt")
            except EOFError:
                break
            except Exception as exc:
                print(f"pynteract: terminal input failed: {exc}", file=sys.stderr)
                return 1
            else:
                try:
                    self.run(code)
                except KeyboardInterrupt:
                    print("\nKeyboardInterrupt")

        print("Exiting interactive shell.")
        return 0

    def _run_startup(self, *, announce: bool) -> int:
        """Run `~/.pynteract/startup.py` once per Shell instance.

        Returns a process-like exit code: 0 on success/no startup, 1 on failure.
        """
        if self._startup_ran:
            if announce and self._startup_has_source and not self._startup_announced:
                print("Running startup...")
                self._startup_announced = True
                if self._startup_failed:
                    sys.stderr.write(self._startup_error_message.rstrip() + "\n")
                    return 1
                print("Ready!")
            return 0 if not self._startup_failed else 1

        self._startup_ran = True
        path = "startup configuration"
        try:
            path = user_config.startup_path()
            if not path.exists():
                return 0
            code = read_script(path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            # Startup is optional; failure to read should not prevent interactive mode.
            sys.stderr.write(f"pynteract: could not read startup file: {path} ({exc})\n")
            return 0

        if not code.strip():
            return 0

        self._startup_has_source = True
        if announce:
            print("Running startup...")
            self._startup_announced = True

        response = self.run(code, silent=True, filename=str(path))
        if response.exception is None:
            if announce:
                print("Ready!")
            return 0

        self._startup_failed = True
        enriched = getattr(response.exception, "enriched_traceback_string", None)
        message = (enriched or response.stderr or "").rstrip()
        self._startup_error_message = f"pynteract: error in startup file: {path}\n{message}".rstrip()
        sys.stderr.write(self._startup_error_message + "\n")
        return 1

    def _reset_startup_state(self) -> None:
        self._startup_ran = False
        self._startup_announced = False
        self._startup_has_source = False
        self._startup_failed = False
        self._startup_error_message = ""

    def ensure_builtins(self):
        """Ensures built-in functions and classes are available in the namespace."""
        # Register builtin magics once per shell lifetime. ensure_builtins() can be
        # called multiple times (e.g. set_namespace), and we don't want to overwrite
        # user-defined magics with the same names.
        if not getattr(self, "_builtin_magics_registered", False):
            try:
                from .builtin_magics import register_builtin_magics

                register_builtin_magics(self)
            finally:
                self._builtin_magics_registered = True

        self.update_namespace(
            __builtins__=builtins,
            display=self.display,
            __magics__=self.magics,
            __shell__=self,
            magic=self.register_magic
        )

    def reset_namespace(self):
        """
        Clears the namespace, retaining only built-in functions and classes.

        This method is useful for resetting the shell to its initial state,
        clearing all user-defined variables and functions while keeping builtins.
        """
        with self._namespace_mutation_scope():
            # Namespace reset is handled by the namespace manager.
            self._ns.reset_module_namespace()

            self.magics = {}
            self._builtin_magics_registered = False
            self._futures.reset()
            self.ensure_builtins()

    def update_namespace(self, *args, **kwargs):
        """
        Dynamically updates the namespace with provided variables or functions.

        Args:
            *args: Dictionaries of items to add to the namespace.
            **kwargs: Key-value pairs to add to the namespace.

        This method allows for real-time modifications to the execution environment,
        adding new variables or functions that will be available in subsequent code executions.
        """
        with self._namespace_mutation_scope():
            self.namespace.update(*args, **kwargs)

    def set_namespace(self, namespace):
        """
        Dynamically sets the namespace reference to a chosen mapping.

        Args:
            namespace: A dict used as the module namespace (reference preserved).
        """
        with self._namespace_mutation_scope():
            if not isinstance(namespace, dict):
                raise TypeError("The Shell's namespace must be a dict (reference preserved, module-like semantics).")
            self._ns.set_namespace(namespace)
            self.ensure_builtins()
            return self.namespace


if __name__=='__main__':
    Shell().interact()
