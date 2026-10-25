from __future__ import annotations

import argparse
import sys
import __main__
from importlib import metadata
from pathlib import Path

from .shell import Shell
from .scripts import read_script, script_context


def build_shell(*, namespace: dict | None = None) -> Shell:
    """Return a Shell bound to a `__main__` module namespace."""
    if namespace is None:
        namespace = __main__.__dict__
    return Shell(module_name="__main__", namespace=namespace)


def _run_script(shell: Shell, script: Path) -> int:
    code = read_script(script)
    try:
        resp = shell.run(code, filename=str(script))
        return 0 if resp.exception is None else 1
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        sys.stderr.write(str(exc.code) + "\n")
        return 1


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

        # Keep script state through -i, but always restore the hosting process.
        with script_context(script_path, script_argv), build_shell(namespace={}) as shell:
            if args.interact:
                startup_exit = shell._run_startup(announce=False)
                if startup_exit != 0:
                    return startup_exit
            try:
                exit_code = _run_script(shell, script_path)
            except (OSError, UnicodeError, SyntaxError) as exc:
                sys.stderr.write(f"pynteract: could not read script: {script_path} ({exc})\n")
                return 2
            if args.no_interact or not args.interact:
                return exit_code
            return int(shell.interact())
    else:
        with build_shell() as shell:
            if args.no_interact:
                return 0
            return int(shell.interact())
