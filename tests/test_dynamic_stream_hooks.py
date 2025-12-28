from __future__ import annotations

from pynteract import Shell


def test_streams_use_latest_shell_hooks_during_run():
    events: list[tuple[str, str]] = []

    def hook1(data: str, _buffer: str, ctx) -> None:
        events.append(("hook1", data))

    def hook2(data: str, _buffer: str, ctx) -> None:
        events.append(("hook2", data))

    shell = Shell(display_mode="none", stdout_hook=hook1)
    shell.run(
        "print('a')\n"
        "shell.hooks['stdout_hook'] = hook2\n"
        "print('b')\n",
        globals={"shell": shell, "hook2": hook2},
    )

    assert ("hook1", "a\n") in events
    assert ("hook2", "b\n") in events
