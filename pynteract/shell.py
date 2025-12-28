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

- `input_hook(code, ctx)` runs before parsing the source string (logging, metrics).
- `pre_run_hook(code, ctx)` lets you rewrite code before it is tokenized/executed.
- `code_block_hook(code_block, ctx)` fires for every AST block (useful for tracing).
- `pre_execute_hook(node, source, ctx)` can mutate AST nodes prior to compilation.
- `post_execute_hook(node, result, ctx)` observes results or exceptions per node.
- `display_hook(result, kwargs, ctx)` overrides how expression values are rendered. kwargs is a dict of any extra args passed to `Shell.display()`.
- `stdout_hook(data, buffer, ctx)` / `stderr_hook(data, buffer, ctx)` redirect output streams to custom handlers.
- `stdin_hook(ctx)` redirect stdin reads to a custom handler (web, CLI, agents).
- `exception_hook(exc, ctx)` is invoked once a run finishes with an error.
- `namespace_change_hook(old, new, locals, ctx)` inspects or vetoes namespace diffs.
- `post_run_hook(response, ctx)` sees the aggregate `ShellResponse` for logging or
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
import contextvars

from collections import OrderedDict
from asttokens import ASTTokens
import subprocess

from typing import Any, Callable, Optional, Union, Literal
from .utils import Thread, short_id
from .ptk import prompt
from .collector import Collector
from .magics import MagicParser, Magic
from . import namespace_utils
from . import traceback_utils
from . import future_utils
from . import user_config
from .context import CAPTURE_STREAMS, RUN_CONTEXT, RunContext, SILENT_STDIO, PENDING_STDIN_PROMPT


class ShellResponse:
    """
    Represents the results of code execution, encapsulating various aspects of the execution.

    Attributes:
        input (str): The original input code.
        processed_input (str): The code after preprocessing.
        stdout (str): Captured standard output.
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
        exception: Optional[Exception] = None
    ) -> None:
        self.input = input
        self.processed_input = processed_input
        self.stdout = stdout
        self.stderr = stderr
        self.result = result
        self.exception = exception

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
        Hooks receive a final `ctx` argument (RunContext), where `ctx.name` matches the
        synthetic/custom filename used for the current run (or any custom routing name).
        This routing context enables advanced late redirection and custom dynamic routing.

        input_hook(code, ctx)
        pre_run_hook(code, ctx) -> processed_code
        code_block_hook(code_block, ctx)
        pre_execute_hook(node, source, ctx) -> node
        post_execute_hook(node, result, ctx)
        display_hook(result, kwargs, ctx)
        stdout_hook(data, buffer, ctx)
        stderr_hook(data, buffer, ctx)
        stdin_hook(ctx) -> str or None
        exception_hook(exc, ctx)
        namespace_change_hook(old_globals, new_globals, locals, ctx)
        post_run_hook(response, ctx) -> response
        add_script_run_ctx_hook(thread, ctx)
        get_script_run_ctx_hook(ctx) -> ctx
        
    """

    def __init__(
        self,
        # Configuration
        namespace: Optional[dict[str, Any]] = None,
        module_name: str | None = None,
        ensure_cwd_on_syspath: bool = True,
        display_mode: Literal['all', 'last', 'none'] = 'last',
        history_size: int = 200,
        # Hooks (passed by name via **hooks; see docs above for supported keys)
        **hooks: Any,
    ) -> None:

        # Hooks registry (kept in a dict to make extension easy and to support
        # dynamic hook replacement during a run).
        invalid = [name for name in hooks.keys() if not name.endswith("_hook")]
        if invalid:
            invalid_text = ", ".join(sorted(invalid))
            raise TypeError(f"Unknown non-hook kwargs: {invalid_text}")
        self.hooks: dict[str, Any] = dict(hooks)

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

        self.display_mode = display_mode
        self.magics= {}
        self._magic_parser=MagicParser()
        self.last_result = None
        self._current_code=None
        # _current_filename already initialized above
        self.history_size=max(history_size,1)
        self.history=OrderedDict()
        self._input_counter=0
        self.session=None
        self.ensure_builtins()
        self._startup_ran = False
        self._startup_announced = False
        self._startup_has_source = False
        self._startup_failed = False
        self._startup_error_message = ""

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

    def _invoke_hook(self, hook: Any, *args: Any, ctx: Any = None) -> Any:
        return hook(*args, ctx)

    @property
    def namespace(self) -> dict[str, Any]:
        """Module-backed execution namespace (canonical)."""
        return self._ns.namespace

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

    def run_system_cmd(self, command):
        """Runs a system command using subprocess.
        Args:
            command (str): The system command to run.
        Returns:
            int: The return code of the command.
        """
        def _stream_output(stream, out_stream):
            for line in iter(stream.readline, ''):
                out_stream.write(line)
                out_stream.flush()
            stream.close()

        process = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
        )

        t_out = Thread(target=_stream_output, args=(process.stdout, sys.stdout))
        t_err = Thread(target=_stream_output, args=(process.stderr, sys.stderr))
        add_script_run_ctx_hook = self.hooks.get("add_script_run_ctx_hook")
        if add_script_run_ctx_hook:
            get_script_run_ctx_hook = self.hooks.get("get_script_run_ctx_hook")
            run_ctx = RUN_CONTEXT.get()
            ctx_val = None
            if get_script_run_ctx_hook:
                ctx_val = self._invoke_hook(get_script_run_ctx_hook, ctx=run_ctx)
            self._invoke_hook(add_script_run_ctx_hook, t_out, ctx_val, ctx=run_ctx)
            self._invoke_hook(add_script_run_ctx_hook, t_err, ctx_val, ctx=run_ctx)
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()

        return process.wait()

    def run_system_cmd_capture(self, command: str) -> str:
        """Runs a system command and returns its stdout (IPython-like `!!`).

        Stderr is streamed to the current sys.stderr so it is still visible/captured.
        """
        completed = subprocess.run(
            command,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.stderr:
            sys.stderr.write(completed.stderr)
            sys.stderr.flush()
        return completed.stdout


    def _execute_node(self, node, source, globals, locals, suppress_result=False, is_last_node=False):
        """
        Execute a single AST node in the given namespace.

        Args:
            node (ast.AST): The AST node to execute.
            source (ASTTokens): The source tokens.
            globals (dict): The global namespace.
            locals (dict): The local namespace.
            suppress_result (bool): Whether to suppress the result display.
            is_last_node (bool): Whether this is the last node in the current execution.

        Returns:
            tuple: Updated (globals, locals) after execution.

        This method is responsible for the actual execution of individual AST nodes.
        It handles both expression and statement nodes, and manages result capturing and display.
        """

        pre_execute_hook = self.hooks.get("pre_execute_hook")
        if pre_execute_hook:
            node = self._invoke_hook(pre_execute_hook, node, source, ctx=RUN_CONTEXT.get())
            if not isinstance(node, ast.AST):
                raise TypeError("pre_execute_hook must return an AST node")
        
        if isinstance(node, ast.Expr):
            compiled_code = compile(
                ast.Expression(node.value),
                filename=self._current_filename,
                mode="eval",
                flags=self._futures.flags,
                dont_inherit=True,
            )
            result = eval(compiled_code, globals,locals)
            self.last_result=result
            if not suppress_result:
                if self.display_mode == 'all' or (self.display_mode == 'last' and is_last_node):
                    self.display(result)
        else:
            self.last_result=None
            compiled_code = compile(
                ast.Module([node], type_ignores=[]),
                filename=self._current_filename,
                mode="exec",
                flags=self._futures.flags,
                dont_inherit=True,
            )
            exec(compiled_code, globals,locals)

        post_execute_hook = self.hooks.get("post_execute_hook")
        if post_execute_hook:
            self._invoke_hook(post_execute_hook, node, self.last_result, ctx=RUN_CONTEXT.get())

        return globals,locals

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

    def _execute_source(self, processed_code: str, *, filename: str, exec_globals: dict, exec_locals):
        source = ASTTokens(processed_code, parse=True, filename=filename)
        nodes = source.tree.body
        self._futures.begin_block()
        for i, node in enumerate(nodes):
            self._futures.process_node(node)

            next_token = source.next_token(node.last_token)
            suppress_result = bool(next_token and next_token.string == ';')
            is_last_node = (i == len(nodes) - 1)

            startpos = node.first_token.startpos
            endpos = next_token.endpos if suppress_result else node.last_token.endpos
            code_block = source.text[startpos:endpos]

            code_block_hook = self.hooks.get("code_block_hook")
            if code_block_hook:
                self._invoke_hook(code_block_hook, code_block, ctx=RUN_CONTEXT.get())

            exec_globals, exec_locals = self._execute_node(
                node, source, exec_globals, exec_locals, suppress_result, is_last_node
            )

        return exec_globals, exec_locals

    def run(self, code, globals=None, locals=None, silent=False, filename=None):
        """
        Execute the given code in the shell environment.

        Args:
            code (str): The Python code to execute.
            globals (dict, optional): Global namespace to use. If None, uses self.namespace.
            locals (dict, optional): Local namespace to use. If None, globals will be used.
            silent (bool): If True, suppress stdout/stderr hook calls for this run (output is still captured).
            filename (str, optional): Custom filename used in tracebacks/history (useful for notebooks/cells).

        Returns:
            ShellResponse: An object containing the results of the execution.
        """
        filename = self._next_filename(filename)
        token_ctx = RUN_CONTEXT.set(RunContext(name=filename))
        token_silent = SILENT_STDIO.set(bool(silent))
        token_prompt = PENDING_STDIN_PROMPT.set(False)
        try:
            input_hook = self.hooks.get("input_hook")
            if input_hook:
                self._invoke_hook(input_hook, code, ctx=RUN_CONTEXT.get())

            exec_globals, exec_locals, is_session_namespace = self._select_exec_context(globals, locals)

            with self._magic_parser._placeholder_scope():
                code = self._magic_parser._parse_system_cmd(code)
                code = self._magic_parser._parse_magics(code)

                pre_run_hook = self.hooks.get("pre_run_hook")
                if pre_run_hook:
                    processed_code = self._invoke_hook(pre_run_hook, code, ctx=RUN_CONTEXT.get())
                else:
                    processed_code = code

                if is_session_namespace:
                    self._ns.set_current_filename(self._current_filename)
                namespace_utils.NamespaceManager.prepare_namespace(
                    exec_globals,
                    filename=filename,
                    module_name=self._ns.module_name if is_session_namespace else None,
                )

                self._current_code = processed_code
                old_globals = dict(exec_globals)
                self.last_result = None

                collector = Collector(self)
                with collector:
                    token_capture = CAPTURE_STREAMS.set((collector.stdout_stream, collector.stderr_stream))
                    try:
                        exec_globals, exec_locals = self._execute_source(
                            processed_code, filename=filename, exec_globals=exec_globals, exec_locals=exec_locals
                        )
                    except:
                        raise
                    finally:
                        CAPTURE_STREAMS.reset(token_capture)

                namespace_change_hook = self.hooks.get("namespace_change_hook")
                if namespace_change_hook:
                    self._invoke_hook(namespace_change_hook, old_globals, exec_globals, exec_locals, ctx=RUN_CONTEXT.get())

                if "__" in exec_globals:
                    exec_globals["___"] = exec_globals["__"]
                if "_" in exec_globals:
                    exec_globals["__"] = exec_globals["_"]
                exec_globals["_"] = self.last_result

                response = ShellResponse(
                    input=code,
                    processed_input=processed_code,
                    stdout=collector.get_stdout(),
                    stderr=collector.get_stderr(),
                    result=self.last_result,
                    exception=collector.exception,
                )

                post_run_hook = self.hooks.get("post_run_hook")
                if post_run_hook:
                    response = self._invoke_hook(post_run_hook, response, ctx=RUN_CONTEXT.get())

                self.add_to_history(filename, response)
                return response
        finally:
            PENDING_STDIN_PROMPT.reset(token_prompt)
            SILENT_STDIO.reset(token_silent)
            RUN_CONTEXT.reset(token_ctx)
    
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
                run_ctx = RUN_CONTEXT.get()
                invoke = getattr(self, "_invoke_hook", None)
                if callable(invoke):
                    invoke(display_hook, obj, kwargs, ctx=run_ctx)
                else:
                    display_hook(obj, kwargs, run_ctx)
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

        loop=True

        def custom_exit():
            nonlocal loop
            loop=False

        self.update_namespace(exit=custom_exit)
        
        while loop:
            try:
                code = prompt(">>> ", prompt_continuation="... ", highlighting="python")
                self.run(code)
            except KeyboardInterrupt:
                print("\nKeyboardInterrupt")
            except EOFError:
                break
            except Exception as e:
                print(f"An error occurred: {e}")

        print("Exiting interactive shell.")
        return 0

    def _run_startup(self, *, announce: bool) -> int:
        """Run `~/.pynteract/startup.py` once per Shell instance.

        Returns a process-like exit code: 0 on success/no startup, 1 on failure.
        """
        if self._startup_ran:
            if announce and self._startup_has_source and not self._startup_announced:
                print("Running startup...")
                if self._startup_failed:
                    sys.stderr.write(self._startup_error_message.rstrip() + "\n")
                    return 1
                print("Ready!")
                self._startup_announced = True
            return 0 if not self._startup_failed else 1

        path = user_config.startup_path()
        self._startup_ran = True

        try:
            if not path.exists():
                return 0
            code = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            # Startup is optional; failure to read should not prevent interactive mode.
            sys.stderr.write(f"pynteract: could not read startup file: {path} ({exc})\n")
            return 0

        if not code.strip():
            return 0

        self._startup_has_source = True
        if announce:
            print("Running startup...")

        response = self.run(code, silent=True, filename=str(path))
        if response.exception is None:
            if announce:
                print("Ready!")
                self._startup_announced = True
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
        self.namespace.update(*args, **kwargs)

    def set_namespace(self, namespace):
        """
        Dynamically sets the namespace reference to a chosen mapping.

        Args:
            namespace: A dict used as the module namespace (reference preserved).
        """
        if not isinstance(namespace, dict):
            raise TypeError("The Shell's namespace must be a dict (reference preserved, module-like semantics).")
        self._ns.set_namespace(namespace)
        self.ensure_builtins()
        return self.namespace
        
if __name__=='__main__':
    Shell().interact()
