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

Optional terminal interactive mode (adds prompt_toolkit):

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

### History

`Shell.history` is an `OrderedDict` of recent `ShellResponse` objects keyed by synthetic/custom filename.
The size is capped by `history_size`.

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

- Escape literal braces with `{{` / `}}`.

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

## Hooks

Hooks are stored in `shell.hooks` (a dict) and are looked up dynamically during execution. Updating a hook
while the shell is running takes effect immediately.

```python
from pynteract import Shell

def stdout_hook(data: str, buffer: str, ctx) -> None:
    print("STDOUT:", data, end="")

shell = Shell(display_mode="none", stdout_hook=stdout_hook)
shell.hooks["stdout_hook"] = stdout_hook  # can be swapped dynamically
```

Hooks receive a final `ctx` argument (`RunContext`) for routing. `ctx.name` matches the synthetic/custom
`filename` of the current run (or another routing name if you override it). This routing context is intended
for advanced late redirection or custom dynamic routing.

## Interactive terminal mode

```python
from pynteract import Shell
Shell().interact()
```

### Persistent history

Interactive sessions store prompt histories under `~/.pynteract/` (or `PYNTERACT_CONFIG_DIR`):

- `history_python.txt`: interactive `>>>` input history
- `history_text.txt`: stdin “text” history used for `input()` reads

### Startup script

If present and non-empty, `~/.pynteract/startup.py` is executed at the start of interactive sessions.

- The startup file is executed via `Shell.run(..., silent=True)` so magics/system commands are supported.
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
from pynteract import Shell

cell_widgets = {}  # e.g. {"cell-42": StdoutTextArea(...)}

def cell_id_from_ctx(ctx) -> str:
    return ctx.name.split(":")[1]  # e.g. "<nb:cell-42:run-7>" -> "cell-42"

def stdout_router(data: str, _buffer: str, ctx) -> None:
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
    **hooks,
)
```

Key methods:

| Method | Signature | Notes |
| --- | --- | --- |
| Execute | `run(code, globals=None, locals=None, silent=False, filename=None) -> ShellResponse` | `silent=True` suppresses stdout/stderr hooks (output still captured). |
| Interactive | `interact() -> int` | Terminal REPL; returns process-like exit code. |
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
| `last_result` | `Any` | Last expression value. |

### `ShellResponse`

Returned by `Shell.run(...)`.

| Field | Type | Meaning |
| --- | --- | --- |
| `input` | `str` | Original source string. |
| `processed_input` | `str` | Expanded source (magics/system commands). |
| `stdout` / `stderr` | `str` | Captured output for the run. |
| `result` | `Any` | Last expression value (depending on `display_mode`). |
| `exception` | `Exception | None` | Exception instance, with enriched traceback attached (when available). |

### `RunContext` and context capture

Hooks receive a `RunContext` object (`ctx`) with:

- `ctx.name`: routing name (by default the synthetic/custom `filename` of the current `run()`).

Use `ctx.name` as the routing key when you need late redirection (background threads) or custom dynamic routing.

Use `shell.capture_context()` to propagate routing/capture context to a new thread:

```python
ctx = shell.capture_context()
threading.Thread(target=lambda: ctx.run(worker)).start()
```

### Hook reference

All hooks are optional. Hook keys live in `shell.hooks` and are passed to `Shell(...)` via `**hooks` (kwargs must end with `_hook`).
When provided, hooks must accept a final `ctx: RunContext` to support late redirection and custom dynamic routing.

| Hook key | Signature | When it runs |
| --- | --- | --- |
| `input_hook` | `input_hook(code: str, ctx) -> None` | Before parsing. |
| `pre_run_hook` | `pre_run_hook(code: str, ctx) -> str` | Before tokenization/execution; can rewrite source. |
| `code_block_hook` | `code_block_hook(code_block: str, ctx) -> None` | For each executed AST block. |
| `pre_execute_hook` | `pre_execute_hook(node, source, ctx) -> ast.AST` | Before compiling a node. |
| `post_execute_hook` | `post_execute_hook(node, result, ctx) -> None` | After a node executes. |
| `display_hook` | `display_hook(obj, kwargs, ctx) -> None` | When displaying expression values. |
| `stdout_hook` | `stdout_hook(data: str, buffer: str, ctx) -> None` | As stdout is flushed. |
| `stderr_hook` | `stderr_hook(data: str, buffer: str, ctx) -> None` | As stderr is flushed. |
| `stdin_hook` | `stdin_hook(ctx) -> str | None` | For stdin reads (`None` = EOF). |
| `exception_hook` | `exception_hook(exc: Exception, ctx) -> None` | When a run finishes with an error. |
| `namespace_change_hook` | `namespace_change_hook(old, new, locals, ctx) -> None` | After a run, with before/after namespaces. |
| `post_run_hook` | `post_run_hook(resp: ShellResponse, ctx) -> ShellResponse` | Final response override/hook. |

### Configuration files

Pynteract stores user-facing state under `~/.pynteract/` (override with `PYNTERACT_CONFIG_DIR`):

- `startup.py`: optional startup file (interactive + `-i` only)
- `history_python.txt`: persistent REPL history
- `history_text.txt`: persistent stdin history

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

To develop terminal interactive mode:

```bash
pip install -e ".[dev,terminal]"
```

## License

MIT. See `LICENSE` file.
