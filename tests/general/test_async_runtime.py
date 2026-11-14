"""Durable contracts for Pynteract's native async runtime."""

import asyncio
import shlex
import shutil
import sys

import pytest

from pynteract import ExecutionInterrupted, Shell, processes, ptk
from pynteract.collector import Collector
from pynteract.magics import MagicParser


@pytest.fixture
def startup(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNTERACT_CONFIG_DIR", str(tmp_path))
    return tmp_path / "startup.py"


async def test_ainteract_executes_top_level_await(startup, monkeypatch):
    from pynteract import shell as shell_module

    entries = iter(["import asyncio\nawait asyncio.sleep(0)\nanswer = 42", EOFError()])

    async def prompt(*args, **kwargs):
        item = next(entries)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(shell_module, "prompt_async", prompt)
    with Shell(display_mode="none") as shell:
        assert await shell.ainteract() == 0
        assert shell.namespace["answer"] == 42


async def test_ainteract_terminal_failure_is_reported_once(
    startup, monkeypatch, capsys,
):
    from pynteract import shell as shell_module

    calls = []

    async def prompt(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("install pynteract[terminal]")

    monkeypatch.setattr(shell_module, "prompt_async", prompt)
    with Shell(display_mode="none") as shell:
        assert await shell.ainteract() == 1
    assert calls == [1]
    assert "pynteract[terminal]" in capsys.readouterr().err


async def test_async_startup_supports_top_level_await_and_restart(startup):
    startup.write_text(
        'import asyncio\nawait asyncio.sleep(0)\nname = "async-startup"\n'
    )
    with Shell(display_mode="none") as shell:
        assert await shell.arestart_session(announce=False) == 0
        assert shell.namespace["name"] == "async-startup"

        shell.update_namespace(exit=lambda: None, transient=1)
        exit_fn = shell.namespace["exit"]
        startup.write_text(
            'import asyncio\nawait asyncio.sleep(0)\nname = "restarted"\n'
        )
        assert await shell.arestart_session(announce=False) == 0
        assert shell.namespace["name"] == "restarted"
        assert "transient" not in shell.namespace
        assert shell.namespace["exit"] is exit_fn


async def test_prompt_async_validates_highlighting():
    with pytest.raises(ValueError, match="highlighting"):
        await ptk.prompt_async(">", highlighting="invalid")


async def test_prompt_async_uses_the_matching_session(monkeypatch):
    calls = []
    requested = []

    class FakeSession:
        async def prompt_async(self, **kwargs):
            calls.append(kwargs)
            return "answer"

    session = FakeSession()

    def get_session(highlighting):
        requested.append(highlighting)
        return session

    monkeypatch.setattr(ptk, "_get_ptk_session", get_session)
    assert await ptk.prompt_async("chat> ", highlighting="text") == "answer"
    assert requested == ["text"]
    assert calls == [{
        "message": "chat> ",
        "multiline": True,
        "prompt_continuation": "",
        "wrap_lines": False,
    }]


async def test_arun_uses_async_magic_implementation_when_registered():
    shell = Shell(display_mode="none")

    def sync_caps(text):
        return "sync:" + text.upper()

    async def async_caps(text):
        await asyncio.sleep(0)
        return "async:" + text.upper()

    shell.register_magic(sync_caps, name="caps", async_func=async_caps)
    assert shell.run("%caps hello").result == "sync:HELLO"
    assert (await shell.arun("%caps hello")).result == "async:HELLO"


async def test_async_only_magic_requires_arun_without_leaking_coroutine():
    shell = Shell(display_mode="none")

    async def async_caps(text):
        await asyncio.sleep(0)
        return text.upper()

    shell.register_magic(async_caps, name="caps")
    response = shell.run("%caps hello")
    assert isinstance(response.exception, TypeError)
    assert "use Shell.arun()" in str(response.exception)
    assert (await shell.arun("%caps hello")).result == "HELLO"


def test_async_syntax_is_lowered_to_awaited_runtime_calls():
    parser = MagicParser()
    with parser._placeholder_scope():
        bang = parser._parse_system_cmd("!echo hello", async_mode=True)
    with parser._placeholder_scope():
        capture = parser._parse_system_cmd("!!echo hello", async_mode=True)
    with parser._placeholder_scope():
        line_magic = parser._parse_magics("%timeit x + 1", async_mode=True)
    with parser._placeholder_scope():
        cell_magic = parser._parse_magics("%%bash\necho hello", async_mode=True)

    assert "await __shell__.arun_system_cmd(" in bang
    assert "await __shell__.arun_system_cmd_capture(" in capture
    assert "await __shell__._acall_magic('timeit', 'line'" in line_magic
    assert "await __shell__._acall_magic('bash', 'cell'" in cell_magic


@pytest.mark.skipif(sys.platform.startswith("win"), reason="requires a POSIX shell")
async def test_async_system_commands_stream_capture_and_render_templates():
    shell = Shell(display_mode="none")
    executable = shlex.quote(sys.executable)

    task = asyncio.create_task(shell.arun(
        f'!{executable} -c "import time; time.sleep(0.1); print(123)"'
    ))
    await asyncio.sleep(0.01)
    assert not task.done()
    streamed = await task

    captured = await shell.arun(
        f'!!{executable} -c "import sys; print(1); print(2, file=sys.stderr)"'
    )
    templated = await shell.arun(
        f'x = 7\n!{executable} -c "print({{x}})"'
    )

    assert streamed.exception is None and "123" in streamed.stdout
    assert captured.exception is None and "1" in captured.result
    assert "2" in captured.stderr
    assert templated.exception is None and "7" in templated.stdout


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
async def test_async_bash_magic_does_not_block_event_loop():
    shell = Shell(display_mode="none")
    task = asyncio.create_task(
        shell.arun("%%bash\nsleep 0.1\nprintf async-bash")
    )

    await asyncio.sleep(0.02)
    assert not task.done()
    response = await task

    assert response.exception is None
    assert response.stdout == "async-bash"
    assert response.result == 0


async def test_async_reader_failure_reaps_process(monkeypatch):
    children = []
    create = processes.asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        return child

    def fail(data, buffer):
        raise RuntimeError("output delivery failed")

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", record)
    shell = Shell(display_mode="none", stdout_hook=fail)
    command = [
        sys.executable, "-c",
        'import time; print("child-output", flush=True); time.sleep(30)',
    ]
    async with Collector(shell) as collector:
        await processes.run_streamed_async(command)

    assert isinstance(collector.exception, RuntimeError)
    assert str(collector.exception) == "output delivery failed"
    assert children[0].returncode is not None


async def test_async_process_cancellation_reaps_child(monkeypatch):
    children = []
    created = asyncio.Event()
    create = processes.asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        created.set()
        return child

    monkeypatch.setattr(processes.asyncio, "create_subprocess_exec", record)
    task = asyncio.create_task(processes.run_streamed_async(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    ))
    await created.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].returncode is not None


async def test_interrupt_async_system_command_reaps_process(monkeypatch):
    children = []
    create = processes.asyncio.create_subprocess_shell

    async def record(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        return child

    ready = asyncio.Event()

    def stdout_hook(data, _buffer):
        if "ready" in data:
            ready.set()

    monkeypatch.setattr(processes.asyncio, "create_subprocess_shell", record)
    shell = Shell(display_mode="none", stdout_hook=stdout_hook)
    child_code = 'import time; print("ready", flush=True); time.sleep(30)'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)}"
    task = asyncio.create_task(shell.arun("!" + command))

    await asyncio.wait_for(ready.wait(), 2)
    assert shell.interrupt()
    with pytest.raises(ExecutionInterrupted):
        await asyncio.wait_for(task, 2)

    assert children[0].returncode is not None
    assert shell.run("42").result == 42
