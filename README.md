# Pynteract

Pynteract is a lightweight, dependency-minimal, embeddable Python “cell shell” with IPython-style ergonomics:
magics (`%`, `%%`), system commands (`!`, `!!`), per-cell filenames for better tracebacks, and a hookable execution pipeline.

It is designed to be used inside notebooks, web apps, AI agents, CLIs, and other embedded environments where you want:
deterministic execution, controllable IO, and rich integration points.

## Table of contents

- [Install](#install)
- [Quick start](#quick-start)
- [Core concepts](#core-concepts)
  - [Execution + display](#execution--display)
  - [Native async execution](#native-async-execution)
  - [Concurrency and process state](#concurrency-and-process-state)
  - [Namespaces + filenames](#namespaces--filenames)
  - [History](#history)
- [Magics and system commands](#magics-and-system-commands)
  - [Registering magics](#registering-magics)
  - [Magic forms](#magic-forms)
  - [Templates](#templates)
  - [System commands](#system-commands)
  - [Built-in magics](#built-in-magics)
- [Hooks](#hooks)
- [Interactive terminal mode](#interactive-terminal-mode)
  - [Persistent history](#persistent-history)
  - [Startup script](#startup-script)
  - [Restarting a session](#restarting-a-session)
- [CLI](#cli)
- [Threads and late output routing](#threads-and-late-output-routing)
- [API reference](#api-reference)
  - [`Shell`](#shell)
  - [`ShellResponse`](#shellresponse)
  - [`RunContext` and context capture](#runcontext-and-context-capture)
  - [Hook reference](#hook-reference)
  - [Configuration files](#configuration-files)
- [Development](#development)
- [License](#license)

## Install

```bash
pip install pynteract
```

Optional terminal interactive mode (adds prompt_toolkit and Pygments):

```bash
pip install "pynteract[terminal]"
```

## Quick start

```python
from pynteract import Shell

shell = Shell(display_mode="none")
resp = shell.run("x = 41 + 1\nx")

assert resp.result == 42
assert resp.stdout == ""
assert resp.stderr == ""
assert resp.exception is None
```

## Core concepts

### Execution + display

- Code is executed node-by-node (AST-level), enabling pre/post hooks per statement/expression.
- `display_mode` controls what expression results are displayed:
  - `"last"` (default): display only the last expression value
  - `"all"`: display every expression value
  - `"none"`: never display expression values
- A semicolon after an expression suppresses display, mirroring IPython: `x;`.

### Native async execution

`await shell.arun(...)` executes in the caller's event loop and task, with
**top-level `await`, `async for` and `async with`**. It shares the namespace,
history, compiler future flags, hooks and `ShellResponse` contract with `run()`.
No hidden thread, nested event loop or serialization is involved.

```python
import asyncio
from pynteract import Shell

async def main():
    shell = Shell(display_mode="none")
    response = await shell.arun(
        "import asyncio\nawait asyncio.sleep(0, result={'answer': 42})"
    )
    assert response.exception is None
    assert response.result == {"answer": 42}
    assert shell.namespace["_"] is response.result

asyncio.run(main())  # In an existing async environment, use await main() instead.
```

Only code compiled for top-level async execution is awaited. An ordinary
expression such as `some_async_function()` returns its coroutine object; write
`await some_async_function()` to execute it. Results are the original Python
objects, and caller `ContextVar` changes follow normal same-task semantics.
`run()` remains synchronous and does not accept top-level await.

**Blocking code still blocks.** A cell's `time.sleep()`, synchronous `input()`,
CPU work, system commands and ordinary hooks run synchronously. For deliberately
blocking cells, opt into a worker yourself:

```python
response = await asyncio.to_thread(shell.run, "blocking_function()")
```

Task cancellation and `KeyboardInterrupt`/`SystemExit` propagate, rather than
becoming cell errors. Cleanup restores capture and run context, but does not
undo namespace mutations or other user-code side effects. Cancellation is
cooperative: it cannot interrupt synchronous blocking code. Child tasks and
threads created by a cell remain the caller's responsibility; await/join them
before the cell returns, particularly when using async output hooks.

### Programmatic interruption

`Shell.interrupt()` requests interruption without closing the shell or replacing
its namespace. It returns `False` when idle. A delivered interruption raises
`ExecutionInterrupted` (a `BaseException`, not an ordinary cell error) from
`run()`/`arun()`; catch it in the host, then reuse the same shell. Mutations and
side effects already performed remain, including object identities.

```python
from pynteract import ExecutionInterrupted

try:
    response = await shell.arun(code)
except ExecutionInterrupted:
    pass  # The shell and its namespace remain usable.
```

For an async run, another host task/thread can call `shell.interrupt()`; it
schedules cancellation on the owning task. External task cancellation still
propagates as `asyncio.CancelledError`. Synchronous code blocks its event loop;
to interrupt it, the host must call from its owning thread (for example a
host-installed Python signal handler). Foreign-thread synchronous interruption
is rejected rather than injecting unsafe exceptions. The library installs no
process-wide signal handlers and does not send OS signals itself.

This is cooperative, best-effort interruption, not forced termination: native
calls can delay it and user code can catch/ignore it. Cleanup follows ordinary
Python exception/cancellation semantics. Asynchronous signal delivery cannot
guarantee every user `finally` block (for example tight single-line loops on
CPython 3.12). Resource-critical hosts should isolate execution in a process and
provide a separate explicit hard-stop fallback; that fallback loses its memory.

### Concurrency and process state

A `Shell` has mutable execution state and rejects overlapping runs from different
tasks or threads with `RuntimeError`; call it sequentially. Inline synchronous
nested runs (for example `%run`) are supported.

Different shells can overlap by default: **use `use_internal_cwd=False` (the
default) and omit the `cwd` argument**. Python-level stdout/stderr and synchronous stdin reads are routed
by task context; unrelated tasks retain the hosting process streams. Async tasks
inherit context, while ordinary `threading.Thread` workers require explicit
context copying. The library's command-reader threads copy context automatically.

Working-directory isolation cannot be task-local: `os.chdir()` changes the whole
process. Runs using opt-in internal-cwd mode, or an explicit `cwd`, reject
overlap with another Pynteract execution. Even ambient-mode concurrent cells must
not mutate cwd. This guard does not protect unrelated host code from a cell's
process-wide changes.

Pynteract is **not a sandbox**. Python modules, environment variables, file
descriptors and process state are shared. Native writes to file descriptors are
not captured, and replacing global streams or using `redirect_stdout` *inside*
overlapping cells is not task-local. Use separate processes for untrusted code,
parallel CPU execution or independent process state.

### Namespaces + filenames

Pynteract runs in a module-backed namespace by default (good `__module__` behavior, better tracebacks).

You can embed it in an existing namespace:

```python
import sys
from pynteract import Shell

shell = Shell(module_name="__main__", namespace=sys.modules["__main__"].__dict__)
```

Each `run()` is assigned a synthetic filename like `<shell-input-3>` (or a custom one via `filename=...`).
This filename is used for:

- tracebacks
- `Shell.history` keys
- hook routing via `RunContext.name` (handy in notebook-like UIs)

### Shell lifetime and source inspection

Use a context manager for short-lived shells, or explicitly call `close()`:

```python
import inspect
from pynteract import Shell

with Shell(silent=True) as shell:
    shell.run("def answer():\n    return 42\n")
    assert "return 42" in inspect.getsource(shell.namespace["answer"])
```

`close()` is idempotent and releases owned `sys.modules` registrations and
interactive `linecache` entries. It restores replaced modules only if the slot
still contains the shell's own module; borrowed modules and later host
replacements are not removed. The CLI closes its shell on every exit path.
Closing during execution is rejected. After closing, execution and structural
namespace changes are rejected, but existing namespace data remains readable.

Sources remain available until close, independently of `history_size`, so live
functions keep inspectable source even after history eviction or reuse of a cell
name. This intentionally retains cell source text for the shell's lifetime.
Colliding cell names and real file paths receive private compilation identifiers;
hooks, history and enriched tracebacks retain the requested public filename.
Existing files and host cache entries are never overwritten. Source inspection
is no longer guaranteed after close (or if the host explicitly clears linecache).

Dropping the last local reference alone is **not** a cleanup contract: registered
module namespaces contain shell helpers, including `__shell__`. Use `close()` or
`with`; caller-owned namespaces can also intentionally retain these references.
This does not undo user mutations, imports, or other process-global side effects.

### History

`Shell.history` is an `OrderedDict` of recent `ShellResponse` objects keyed by synthetic/custom filename.
The size is capped by `history_size`.

### Working directory modes

By default, `Shell(use_internal_cwd=False)` shares the host process cwd without
calling `os.chdir()` implicitly. `shell.cwd` reflects `os.getcwd()`. Setting
`shell.cwd`, using `%cd` or `os.chdir()`, or supplying `run(..., cwd=...)` changes
the **whole process** cwd and leaves it wherever the run ended. Avoid such
mutations while concurrent code depends on relative paths.

Opt into `Shell(use_internal_cwd=True)` to save a shell cwd between runs and
restore the caller process cwd after each run. This temporarily changes the
process cwd, not a task-local directory; it requires exclusive Pynteract execution
and cannot isolate unrelated host code. An explicit `cwd` argument also requires
exclusive execution, in either mode. Truly independent OS working directories
require separate processes.

## Magics and system commands

### Registering magics

```python
from pynteract import Shell

shell = Shell(display_mode="none")

@shell.register_magic(name="caps", mode="both")  # "line" | "cell" | "both"
def caps(text: str) -> str:
    return text.upper()

assert shell.run("%caps hello").result == "HELLO"
```

### Magic forms

- Line magic: `%name rest of line`
- Cell magic: `%%name` on the first line; the remaining cell body is passed as a string
- Inline magic: `x = %name rest of line` (also works after `;`)

### Templates

Inside magics and system commands, `{expr}` is evaluated in the current namespace and replaced with `str(value)`.

- Escape literal braces with `{{` / `}}`. Expressions support nested dictionaries,
  sets and quoted braces.
- Templates execute trusted Python, not sandboxed expressions. Curated builtin
  names do not restrict the capabilities of namespace objects.
- Cell-magic bodies are not parsed as line magics or system escapes; template
  expansion still applies. Use escaped braces for literal shell/Python braces.

### System commands

- `!cmd ...` runs a system command and streams stdout/stderr into the captured streams.
- `!!cmd ...` runs a system command, captures stdout, and returns it as the cell result (stderr still streams).

```python
shell = Shell(display_mode="none")
shell.run('!python3 -c "print(123)"').stdout
shell.run('!!python3 -c "print(456)"').result
```

### Built-in magics

Pynteract ships a small set of “IPython-like” convenience magics (registered automatically by `Shell.ensure_builtins()`):

- `%pwd`, `%cd [path|-]`, `%ls [path]`
- `%env`, `%env KEY`, `%env KEY=value`, `%env -u KEY`
- `%who`, `%whos`
- `%run [-i] script.py [args...]`
- `%time <code>`, `%timeit [-n N] [-r R] <code>`
- `%%bash`

`%timeit` accepts positive `-n` / `-r` counts before the Python code. It preserves
Python quotes and spacing rather than parsing the code as shell arguments.
Line-magic arguments may be separated from the name by spaces or tabs.

`%run` executes in a fresh `__main__` namespace and merges user symbols (including
those defined before an error); `-i` uses the current namespace. Script failures
become failures of the calling cell, and nested stdout/stderr are included in its
response without delivering stream hooks or printing the same error twice.
Successful `%run` returns `None`. Scripts respect Python encoding cookies/BOMs and
can import siblings. Script argv, import path and the host `__main__` registration
are restored on exit, including control-flow exceptions; these globals are still
process-wide, not safe isolation from unrelated threads. Imported modules remain
cached as in ordinary Python. The CLI maps `SystemExit` to an exit status and
preserves script state through `-i` only until the interactive session exits.

System commands are blocking, even inside `arun()`. Reader/hook failures propagate
after child cleanup. POSIX cleanup terminates the owned process group; on Windows
it terminates only the direct child (descendant-tree cleanup is not guaranteed).

## Hooks

Hooks are stored in `shell.hooks` (a dict) and are looked up dynamically during execution. Updating a hook
while the shell is running takes effect immediately.

```python
import sys
from pynteract import Shell

def stdout_hook(data: str, buffer: str) -> None:
    sys.__stdout__.write(data)  # explicit output to the real terminal

shell = Shell(display_mode="none", stdout_hook=stdout_hook)
shell.hooks["stdout_hook"] = stdout_hook  # can be swapped dynamically
```

Hooks receive only their payload. Import `get_run_ctx` from `pynteract` when routing is needed:
`get_run_ctx().name` matches the current run filename. Outside execution, `get_run_ctx()` returns `None`.
Context follows async tasks and explicitly copied thread contexts.

### Async output hooks

Hosts handling untrusted amounts of output can opt into bounded capture:

```python
shell = Shell(capture_limit=256 * 1024, hook_buffer=False,
              stdout_hook=lambda chunk, _: consume(chunk))
```

`capture_limit` is a nonnegative character count **per stream, per run** (default
`None`, unlimited). Capture and hook delivery retain only this prefix, followed
once by `[output truncated: capture limit reached]`; execution continues. The
marker is additional to the limit. `response.output_stats` contains `written_chars`
(excluding the marker) and `truncated` for stdout/stderr. No tail is retained after
capture overflow; use a file for complete logs. `hook_buffer=False` passes `""` as
the hook's second argument rather than materializing cumulative snapshots. It
avoids quadratic copying for chunk-only consumers, including the default mirrors.
With a limit, async hook queues retain at most the accepted prefix plus marker;
slow consumers cannot build an unlimited output backlog. Default unlimited
capture/cumulative-hook semantics remain unchanged. This bounds library-owned
capture, not caller-created strings, custom hooks, user threads or process RSS.

Only `stdout_hook` and `stderr_hook` may be async (`async def`, including async
`__call__` objects), and only with `arun()` or `async with Collector(...)`.
All other hooks, magics and stdin providers are synchronous; accidental awaitable
hook returns raise `TypeError` instead of leaking an unawaited coroutine.

```python
import asyncio
from pynteract import Shell, get_run_ctx

async def main():
    chunks = []

    async def on_stdout(data, buffer):
        await asyncio.sleep(0)  # e.g. await delivery to an embedding UI
        chunks.append((get_run_ctx().name, data))

    shell = Shell(display_mode="none", stdout_hook=on_stdout)
    response = await shell.arun("print('hello')", filename="cell-1")
    assert response.stdout == "hello\n"
    assert chunks == [("cell-1", "hello\n")]  # already drained, no extra sleep

asyncio.run(main())
```

Synchronous writes enqueue async hook deliveries without waiting. One worker per
run processes them in emission order across stdout/stderr; hook replacement
applies to future chunks, not ones already queued. Hook output is routed to the
host rather than recursively recaptured, while `get_run_ctx()` stays available.
`arun()` waits for pending deliveries before the namespace/post-run hooks and
before returning. After an ordinary delivery error it drains the remaining
chunks, then raises the first error to the caller (a host delivery failure, not a
`ShellResponse.exception`). Cancellation cancels and awaits the owned worker;
undelivered chunks may be discarded. Hooks must cooperate with cancellation.
`silent=True` suppresses deliveries but still captures text. Async delivery ends
with the run; it is not a background notification service.


## Interactive terminal mode

```python
from pynteract import Shell
Shell().interact()
```

Use **Alt+Enter** to submit, **Enter** for a new line, and **Tab / Shift+Tab**
for indentation. **Ctrl+C** copies a selection, or interrupts when there is none;
**Ctrl+X / Ctrl+V** cut and paste. Missing terminal dependencies or an input
initialization failure produce one diagnostic and a non-zero status, not a retry loop.

### Persistent history

Interactive sessions store prompt histories under `~/.pynteract/` (or `PYNTERACT_CONFIG_DIR`):

- `history_python.txt`: interactive `>>>` input history
- `history_text.txt`: stdin “text” history used for `input()` reads

### Startup script

If present and non-empty, `~/.pynteract/startup.py` is executed at the start of interactive sessions.

- The startup file is read as Python source (encoding cookies and BOMs supported),
  then executed once per Shell via `Shell.run(..., silent=True)`, so magics/system
  commands are supported. Restarting the session explicitly reruns it.
- An unreadable or undecodable optional startup file emits a warning and is skipped;
  its bytes are never silently replaced.
- If the startup fails, Pynteract prints the enriched traceback and exits (non-zero status in the CLI).
- When using `pynteract -i script.py`, the startup script runs before the script (same namespace kept for the REPL).

### Restarting a session

In interactive mode, you can reset the namespace and rerun startup:

```python
__shell__.restart_session()
```

## CLI

Installing the package provides a `pynteract` executable.

```bash
# Interactive session
pynteract

# Run a script (similar to `python script.py`)
pynteract path/to/script.py [args...]

# Run a script, then enter interactive mode with the same namespace
pynteract -i path/to/script.py [args...]
```

## Threads and late output routing

To keep output from background threads routed to the originating cell/run, call `shell.enable_stdio_proxy()` once,
then capture and propagate a `contextvars.Context`:

```python
import threading, time
from pynteract import Shell

shell = Shell(display_mode="none")
shell.enable_stdio_proxy()

def worker():
    for i in range(3):
        print(f"tick {i}")
        time.sleep(0.1)

shell.run("print('cell start')", filename="<cell-1>")
ctx = shell.capture_context()  # captures ctx.name == "<cell-1>"
threading.Thread(target=lambda: ctx.run(worker), daemon=True).start()
```

### Notebook-style “late streaming” example

In a notebook UI, you typically want each cell to own its own stdout/stderr widget, and you want background threads
spawned by a cell to keep streaming into that same widget even after the cell finished.

Use the run `filename=` (exposed as `ctx.name` to hooks) as your routing key:

```python
from pynteract import Shell, get_run_ctx

cell_widgets = {}  # e.g. {"cell-42": StdoutTextArea(...)}

def cell_id_from_ctx(ctx) -> str:
    return ctx.name.split(":")[1]  # e.g. "<nb:cell-42:run-7>" -> "cell-42"

def stdout_router(data: str, _buffer: str) -> None:
    ctx = get_run_ctx()
    cell_widgets[cell_id_from_ctx(ctx)].append(data)

shell = Shell(display_mode="none", stdout_hook=stdout_router)
shell.enable_stdio_proxy()

def run_cell(cell_id: str, run_no: int, code: str) -> None:
    shell.run(code, filename=f"<nb:{cell_id}:run-{run_no}>")
```

## API reference

### `Shell`

Constructor:

```python
Shell(
    namespace: dict | None = None,
    module_name: str | None = None,
    ensure_cwd_on_syspath: bool = True,
    display_mode: Literal["all", "last", "none"] = "last",
    history_size: int = 200,
    silent: bool = False,
    use_internal_cwd: bool = False,
    **hooks,
)
```

Key methods:

| Method | Signature | Notes |
| --- | --- | --- |
| Execute | `run(code, globals=None, locals=None, silent=None, filename=None, cwd=None) -> ShellResponse` | `cwd` selects the run working directory. By default (`use_internal_cwd=False`), runs share/mutate the ambient process cwd. Opt-in `True` temporarily applies the saved shell cwd and restores the caller cwd; both it and an explicit `cwd` require exclusive execution. |
| Async execute | `await arun(code, globals=None, locals=None, silent=None, filename=None, cwd=None) -> ShellResponse` | Native-loop counterpart with top-level await; sync work still blocks. |
| Interactive | `interact() -> int` | Terminal REPL; returns process-like exit code. |
| Lifetime | `close()` / `with Shell() as shell:` | Releases owned module/source registrations; `closed` reports state. |
| Restart | `restart_session(rerun_startup=True, announce=True) -> int` | Resets namespace and (optionally) reruns startup. |
| Namespace | `update_namespace(**kwargs)` | Adds symbols to the execution namespace. |
| Namespace | `set_namespace(namespace: dict)` | Switches to a different dict namespace. |
| Namespace | `reset_namespace()` | Clears user symbols and resets `__future__` flags. |
| Magics | `register_magic(func=None, *, name=None, mode="both")` | Decorator or direct call; mode `"line"|"cell"|"both"`. |
| Threads | `enable_stdio_proxy()` | Installs a routing proxy on `sys.stdout`/`sys.stderr` for late-thread capture. |
| Threads | `capture_context(name=None) -> contextvars.Context` | Capture routing context for another thread. |
| Builtins | `ensure_builtins()` | (Re)adds `__shell__`, `__magics__`, `display`, and built-in magics. |

Important public attributes:

| Attribute | Type | Meaning |
| --- | --- | --- |
| `namespace` | `dict` | Module-backed execution namespace. |
| `hooks` | `dict[str, Any]` | Hook registry; updated dynamically. |
| `magics` | `dict[str, Any]` | Registered magics. |
| `history` | `OrderedDict[str, ShellResponse]` | Recent run history keyed by filename. |
| `cwd` | `str` | Current working directory used by `run()`/`arun()`: shell-local in internal-cwd mode, process cwd in ambient mode. |
| `last_result` | `Any` | Last expression value. |
| `silent` | `bool` | Instance default for suppressing stdout/stderr hooks while still capturing output. |
| `capture_limit` | `int \| None` | Per-stream character limit; `None` keeps all output. |
| `hook_buffer` | `bool` | Whether hooks receive cumulative text as their second argument. |

### `ShellResponse`

Returned by `Shell.run(...)` and `await Shell.arun(...)`.

| Field | Type | Meaning |
| --- | --- | --- |
| `input` | `str` | Original source string. |
| `processed_input` | `str` | Expanded source (magics/system commands). |
| `stdout` / `stderr` | `str` | Captured output for the run. |
| `output_stats` | `dict` | Per-stream `written_chars` and capture `truncated` flag. |
| `result` | `Any` | Value of the final executed node if it is an expression; otherwise `None`. `display_mode` controls rendering, not this value. |
| `exception` | `Exception | None` | Exception instance, with enriched traceback attached (when available). |

Enriched tracebacks (`exception.enriched_traceback_string` and
`exception.enriched_traceback`) start at the first compiled cell frame and keep
all subsequent calls, including library frames. This applies to sync/async cells,
exception chains and exception groups. Compilation errors show Python's source
diagnostics rather than the parser/compiler stack. Errors outside cell execution
(such as a failing pre-execution hook) retain their stack for diagnosis. The
original `exception.__traceback__` remains untouched for technical debugging.

### `RunContext` and context capture

`from pynteract import RunContext, get_run_ctx` exposes the current context without
adding an argument to every hook. `get_run_ctx()` returns a `RunContext` during
execution, or `None` outside it, with:

- `ctx.name`: routing name (by default the synthetic/custom `filename` of the current `run()`).

Use `ctx.name` as the routing key when you need late redirection (background threads) or custom dynamic routing.

Use `shell.capture_context()` to propagate routing/capture context to a new thread:

```python
ctx = shell.capture_context()
threading.Thread(target=lambda: ctx.run(worker)).start()
```

### Hook reference

All hooks are optional. Hook keys live in `shell.hooks` and are passed to `Shell(...)` via `**hooks` (kwargs must end with `_hook`).
Hooks accept only the payload arguments below. Read `get_run_ctx()` inside a hook if needed.
There is no legacy-signature detection: remove the former final `ctx` argument when migrating.

| Hook key | Signature | When it runs |
| --- | --- | --- |
| `input_hook` | `input_hook(code: str) -> None` | Before parsing. |
| `pre_run_hook` | `pre_run_hook(code: str) -> str` | Before tokenization/execution; can rewrite source. |
| `code_block_hook` | `code_block_hook(code_block: str) -> None` | For each executed AST block. |
| `pre_execute_hook` | `pre_execute_hook(node, source) -> ast.AST` | Before compiling a node. |
| `post_execute_hook` | `post_execute_hook(node, result) -> None` | After a node executes. |
| `display_hook` | `display_hook(obj, kwargs) -> None` | When displaying expression values. |
| `stdout_hook` | `stdout_hook(data: str, buffer: str) -> None` | As stdout is flushed. |
| `stderr_hook` | `stderr_hook(data: str, buffer: str) -> None` | As stderr is flushed. |
| `stdin_hook` | `stdin_hook() -> str | None` | For stdin reads (`None` = EOF). |
| `exception_hook` | `exception_hook(exc: Exception) -> None` | When a run finishes with an error. |
| `namespace_change_hook` | `namespace_change_hook(old, new, locals) -> None` | After a run, with before/after namespaces. |
| `post_run_hook` | `post_run_hook(resp: ShellResponse) -> ShellResponse` | Final response override/hook. |
| `get_script_run_ctx_hook` | `get_script_run_ctx_hook() -> Any` | Obtain a host-specific context before starting command reader threads. |
| `add_script_run_ctx_hook` | `add_script_run_ctx_hook(thread, script_ctx) -> None` | Attach that host context to each reader thread. |

The `script_ctx` payload in the last two hooks belongs to the embedding host
(e.g. Streamlit), not to Pynteract's `RunContext`.

Ordinary cell exceptions are captured in `ShellResponse.exception`. Interpreter
control flow (`KeyboardInterrupt`, `SystemExit`) and async cancellation propagate;
streams and the execution context are restored even when hooks fail. Errors in
host setup/finalization hooks or async output delivery propagate to the caller;
ordinary errors during captured execution are stored in the response.

### Configuration files

Pynteract stores user-facing state under `~/.pynteract/` (override with `PYNTERACT_CONFIG_DIR`):

- `startup.py`: optional startup file (interactive + `-i` only)
- `history_python.txt`: persistent REPL history
- `history_text.txt`: persistent stdin history

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q -W error::RuntimeWarning
```

To develop terminal interactive mode:

```bash
pip install -e ".[dev,terminal]"
```

CI runs the regression suite on Linux with Python 3.11–3.14, including the
terminal extra. Terminal tests exercise bindings/history with no real TTY.
This matrix is configured in `.github/workflows/ci.yml`; adding the workflow
does not mean a remote run has already passed, nor does it validate other OSes.

Build and exercise the wheel independently of the source-tree import:

```bash
python -m pip install build
python -m build
python tests/wheel_smoke.py dist/*.whl
```

The default build makes a wheel from the sdist; the smoke script loads that wheel
directly (no package installation) and checks sync/async execution, magics,
source introspection and the CLI entry point. Build dependencies must already be
available when using `--no-isolation`.

## License

MIT. See `LICENSE` file.
