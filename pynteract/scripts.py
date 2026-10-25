"""Python script IO and temporary process state shared by %run and the CLI."""
from contextlib import contextmanager
from pathlib import Path
import sys
import tokenize


def read_script(path: Path) -> str:
    """Respect Python encoding cookies and UTF-8 BOMs."""
    with tokenize.open(path) as source:
        return source.read()


@contextmanager
def script_context(path: Path, argv: list[str]):
    """Restore hosting state even on SystemExit or KeyboardInterrupt.

    Like script execution itself, these process globals are not task-local.
    Modules imported by the script intentionally remain in sys.modules.
    """
    old_argv, old_path = sys.argv, sys.path
    saved_path = old_path[:]
    old_main = sys.modules.get('__main__')
    try:
        sys.argv = argv
        sys.path.insert(0, str(path.parent))
        yield
    finally:
        sys.argv = old_argv
        old_path[:] = saved_path
        sys.path = old_path
        if old_main is None:
            sys.modules.pop('__main__', None)
        else:
            sys.modules['__main__'] = old_main
