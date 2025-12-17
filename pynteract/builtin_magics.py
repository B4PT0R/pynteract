from __future__ import annotations

import ast
import os
import shlex
import subprocess
import sys
import time
import timeit
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .utils import Thread
from . import namespace_utils


if TYPE_CHECKING:
    from .shell import Shell


def register_builtin_magics(shell: "Shell") -> None:
    """Register a small set of commonly-used IPython-like magics on `shell`."""

    @shell.register_magic(name="pwd", mode="line")
    def _pwd(_: str) -> str:
        return os.getcwd()

    prev_cwd: dict[str, str] = {"value": os.getcwd()}

    @shell.register_magic(name="cd", mode="line")
    def _cd(arg: str) -> str:
        target = (arg or "").strip()
        if not target:
            target = os.path.expanduser("~")
        elif target == "-":
            target = prev_cwd["value"]
        target = os.path.expanduser(os.path.expandvars(target))
        prev_cwd["value"] = os.getcwd()
        os.chdir(target)
        return os.getcwd()

    @shell.register_magic(name="ls", mode="line")
    def _ls(arg: str) -> str:
        parts = shlex.split(arg) if arg.strip() else []
        path = Path(parts[0]) if parts else Path(".")
        entries = sorted(path.iterdir(), key=lambda p: p.name)
        return "\n".join(p.name + ("/" if p.is_dir() else "") for p in entries)

    @shell.register_magic(name="env", mode="line")
    def _env(arg: str) -> Any:
        text = (arg or "").strip()
        if not text:
            return dict(os.environ)
        if text.startswith("-u "):
            key = text[3:].strip()
            os.environ.pop(key, None)
            return ""
        if "=" in text:
            key, value = text.split("=", 1)
            os.environ[key] = value
            return value
        return os.environ.get(text, "")

    def _user_symbols() -> dict[str, Any]:
        ns = shell.namespace
        hidden_prefixes = ("__",)
        hidden_names = {"_", "__", "___", "__builtins__", "__magics__", "__shell__", "display", "magic"}
        return {
            k: v
            for k, v in ns.items()
            if k not in hidden_names and not any(k.startswith(p) for p in hidden_prefixes)
        }

    @shell.register_magic(name="who", mode="line")
    def _who(_: str) -> str:
        names = sorted(_user_symbols().keys())
        return " ".join(names)

    @shell.register_magic(name="whos", mode="line")
    def _whos(_: str) -> str:
        rows = [("Name", "Type", "Repr")]
        for name, value in sorted(_user_symbols().items(), key=lambda kv: kv[0]):
            rep = repr(value)
            if len(rep) > 60:
                rep = rep[:57] + "..."
            rows.append((name, type(value).__name__, rep))
        widths = [max(len(r[i]) for r in rows) for i in range(3)]
        return "\n".join(
            f"{r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  {r[2]}" for r in rows
        )

    def _compile_for_exec(code: str, filename: str) -> tuple[object, bool]:
        tree = ast.parse(code, filename=filename, mode="exec")
        is_expr = len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
        flags = getattr(shell, "_futures").flags
        if is_expr:
            compiled = compile(
                ast.Expression(tree.body[0].value),
                filename=filename,
                mode="eval",
                flags=flags,
                dont_inherit=True,
            )
        else:
            compiled = compile(tree, filename=filename, mode="exec", flags=flags, dont_inherit=True)
        return compiled, is_expr

    @shell.register_magic(name="time", mode="line")
    def _time(arg: str) -> str:
        code = (arg or "").strip()
        if not code:
            return "Usage: %time <code>"
        filename = "<pynteract-%time>"
        compiled, is_expr = _compile_for_exec(code, filename)
        start = time.perf_counter()
        if is_expr:
            eval(compiled, shell.namespace, shell.namespace)
        else:
            exec(compiled, shell.namespace, shell.namespace)
        elapsed = time.perf_counter() - start
        return f"Wall time: {elapsed:.6f}s"

    @shell.register_magic(name="timeit", mode="line")
    def _timeit(arg: str) -> str:
        parts = shlex.split(arg) if arg.strip() else []
        n = 1000
        r = 3
        code_parts: list[str] = []
        i = 0
        while i < len(parts):
            if parts[i] == "-n" and i + 1 < len(parts):
                n = int(parts[i + 1])
                i += 2
                continue
            if parts[i] == "-r" and i + 1 < len(parts):
                r = int(parts[i + 1])
                i += 2
                continue
            code_parts.append(parts[i])
            i += 1
        code = " ".join(code_parts).strip()
        if not code:
            return "Usage: %timeit [-n N] [-r R] <code>"

        filename = "<pynteract-%timeit>"
        compiled, is_expr = _compile_for_exec(code, filename)

        def run_once() -> None:
            if is_expr:
                eval(compiled, shell.namespace, shell.namespace)
            else:
                exec(compiled, shell.namespace, shell.namespace)

        timer = timeit.Timer(run_once)
        runs = timer.repeat(repeat=r, number=n)
        best = min(runs) / n
        return f"Best of {r}: {best:.6g}s per loop"

    @shell.register_magic(name="bash", mode="cell")
    def _bash_cell(script: str) -> int:
        process = subprocess.Popen(
            ["bash", "-lc", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        def _stream_output(stream, out_stream):
            for line in iter(stream.readline, ""):
                out_stream.write(line)
                out_stream.flush()
            stream.close()

        t_out = Thread(target=_stream_output, args=(process.stdout, sys.stdout))
        t_err = Thread(target=_stream_output, args=(process.stderr, sys.stderr))
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()
        return process.wait()

    @shell.register_magic(name="run", mode="line")
    def _run(arg: str) -> str:
        """Execute a Python script, IPython-style.

        - `%run script.py [args...]` runs in a fresh `__main__` namespace, then merges symbols into the current shell.
        - `%run -i script.py [args...]` runs in the current shell namespace.
        """
        parts = shlex.split(arg) if arg.strip() else []
        if not parts:
            return "Usage: %run [-i] script.py [args...]"

        interactive = False
        if parts and parts[0] == "-i":
            interactive = True
            parts = parts[1:]

        if not parts:
            return "Usage: %run [-i] script.py [args...]"

        script_path = Path(os.path.expandvars(os.path.expanduser(parts[0]))).resolve()
        argv = [str(script_path), *parts[1:]]

        try:
            source = script_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise FileNotFoundError(f"Could not read script: {script_path}") from exc

        saved_argv = sys.argv[:]
        saved_main = sys.modules.get("__main__")
        saved_file = shell.namespace.get("__file__")

        try:
            sys.argv = argv
            if interactive:
                target_ns = shell.namespace
                shell.run(source, globals=target_ns, locals=target_ns, filename=str(script_path))
            else:
                target_ns: dict[str, Any] = {"__name__": "__main__"}
                for key in ("__builtins__", "display", "__magics__", "__shell__", "magic"):
                    if key in shell.namespace:
                        target_ns[key] = shell.namespace[key]

                namespace_utils.NamespaceManager.prepare_namespace(
                    target_ns, filename=str(script_path), module_name="__main__"
                )
                shell.run(source, globals=target_ns, locals=target_ns, filename=str(script_path))

                hidden = {
                    "__builtins__",
                    "__doc__",
                    "__file__",
                    "__name__",
                    "__package__",
                    "__spec__",
                    "__cached__",
                    "__loader__",
                    "__magics__",
                    "__shell__",
                    "display",
                    "magic",
                    "_",
                    "__",
                    "___",
                }
                for name, value in target_ns.items():
                    if name in hidden or name.startswith("__"):
                        continue
                    shell.namespace[name] = value
        finally:
            sys.argv = saved_argv
            if saved_file is not None:
                shell.namespace["__file__"] = saved_file
            if saved_main is None:
                sys.modules.pop("__main__", None)
            else:
                sys.modules["__main__"] = saved_main

        return ""
