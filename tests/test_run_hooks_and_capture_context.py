from __future__ import annotations

from pynteract import Shell


def test_stdout_hook_receives_run_context_with_filename():
    seen: list[tuple[str, str]] = []

    def hook(data: str, _buffer: str, ctx) -> None:
        seen.append((ctx.name if ctx else "none", data))

    shell = Shell(display_mode="none", stdout_hook=hook)
    shell.run("print('x')", filename="<cell-1#1>")
    shell.run("print('y')", filename="<cell-1#2>")

    assert ("<cell-1#1>", "x\n") in seen
    assert ("<cell-1#2>", "y\n") in seen


def test_capture_context_routes_late_thread_output_to_run_hook():
    seen: list[tuple[str, str]] = []

    def hook(data: str, _buffer: str, ctx) -> None:
        seen.append((ctx.name if ctx else "none", data))

    shell = Shell(display_mode="none", stdout_hook=hook)
    shell.enable_stdio_proxy()

    resp = shell.run(
        "import threading, time\n"
        "ctx = __shell__.capture_context()\n"
        "def worker():\n"
        "    time.sleep(1)\n"
        "    print('late')\n"
        "t = threading.Thread(target=lambda: ctx.run(worker))\n"
        "t.start()\n",
        filename="<cell-2#1>",
    )
    assert resp.exception is None

    shell.namespace["t"].join()
    assert ("<cell-2#1>", "late\n") in seen


def test_capture_context_can_override_filename_for_routing():
    seen: list[tuple[str, str]] = []

    def hook(data: str, _buffer: str, ctx) -> None:
        seen.append((ctx.name if ctx else "none", data))

    shell = Shell(display_mode="none", stdout_hook=hook)
    shell.enable_stdio_proxy()

    resp = shell.run(
        "import threading\n"
        "ctx_default = __shell__.capture_context()\n"
        "ctx_other = __shell__.capture_context(name='<cell-override>')\n"
        "def a():\n"
        "    print('a')\n"
        "def b():\n"
        "    print('b')\n"
        "t1 = threading.Thread(target=lambda: ctx_default.run(a))\n"
        "t2 = threading.Thread(target=lambda: ctx_other.run(b))\n"
        "t1.start(); t2.start(); t1.join(); t2.join()\n",
        filename="<cell-main>",
    )
    assert resp.exception is None
    assert ("<cell-main>", "a\n") in seen
    assert ("<cell-override>", "b\n") in seen
