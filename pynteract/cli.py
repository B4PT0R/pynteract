from __future__ import annotations

import argparse
import sys
import __main__
from importlib import metadata
from pathlib import Path

from .shell import Shell


def build_shell(*, namespace: dict | None = None) -> Shell:
    """Return a Shell bound to a `__main__` module namespace."""
    if namespace is None:
        namespace = __main__.__dict__
    return Shell(module_name="__main__", namespace=namespace)


def _run_script(shell: Shell, script: Path, argv: list[str]) -> int:
    code = script.read_text(encoding="utf-8", errors="replace")
    try:
        sys.argv = argv
        resp = shell.run(code, filename=str(script))
        return 0 if resp.exception is None else 1
    finally:
        # Caller decides whether to restore sys.argv / sys.modules['__main__'].
        pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pynteract", description="Interactive pynteract shell")
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print version and exit.",
    )
    parser.add_argument(
        "-i",
        "--interact",
        action="store_true",
        help="Enter interactive mode after running a script.",
    )
    parser.add_argument(
        "script",
        nargs="?",
        help="Python script to run.",
    )
    parser.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-interact",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        try:
            version = metadata.version("pynteract")
        except Exception:
            version = "unknown"
        sys.stdout.write(f"{version}\n")
        return 0

    if args.script:
        script_path = Path(args.script)
        if not script_path.exists():
            sys.stderr.write(f"pynteract: no such file: {script_path}\n")
            return 2
        script_path = script_path.resolve()
        script_argv = [str(script_path), *list(args.script_args or [])]

        # Run scripts in a fresh __main__ namespace (like `python script.py` / IPython %run).
        saved_argv = sys.argv[:]
        saved_main = sys.modules.get("__main__")
        shell = build_shell(namespace={})
        sys.argv = script_argv

        if args.interact:
            startup_exit = shell._run_startup(announce=False)
            if startup_exit != 0:
                sys.argv = saved_argv
                if saved_main is None:
                    sys.modules.pop("__main__", None)
                else:
                    sys.modules["__main__"] = saved_main
                return startup_exit

        exit_code = _run_script(shell, script_path, script_argv)

        if args.no_interact or not args.interact:
            sys.argv = saved_argv
            if saved_main is None:
                sys.modules.pop("__main__", None)
            else:
                sys.modules["__main__"] = saved_main
            return exit_code
        # Else: keep script argv and __main__ module and fall through into interactive.
    else:
        shell = build_shell()
        if args.no_interact:
            return 0

    return int(shell.interact())
