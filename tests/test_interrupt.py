"""Host interruption must not destroy the shell or stop its event loop."""
import asyncio
import signal
import threading

import pytest

from pynteract import ExecutionInterrupted, Shell, get_run_ctx


def test_interrupt_idle_and_sync_finally():
    shell = Shell()
    assert shell.interrupt() is False
    shell.namespace['stop'] = shell.interrupt
    with pytest.raises(ExecutionInterrupted):
        shell.run('kept = []\ntry:\n    stop()\nfinally:\n    kept.append("cleaned")')
    assert shell.run('kept').result == ['cleaned']
    assert get_run_ctx() is None
    assert shell.interrupt() is False


@pytest.mark.skipif(not hasattr(signal, 'SIGALRM'), reason='POSIX signal integration')
def test_interrupt_sync_loop_from_host_signal():
    shell = Shell()
    previous = signal.signal(signal.SIGALRM, lambda *_: shell.interrupt())
    try:
        signal.setitimer(signal.ITIMER_REAL, .05)
        with pytest.raises(ExecutionInterrupted):
            shell.run('counter = 0\ntry:\n    while True:\n        counter += 1\nfinally:\n    cleaned = True')
        assert shell.namespace['counter'] > 0
        assert shell.run('cleaned').result is True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize('from_thread', [False, True])
async def test_interrupt_await_preserves_namespace_and_task(from_thread):
    shell = Shell()
    ready = asyncio.Event()
    shell.namespace.update(asyncio=asyncio, ready=ready)

    async def run():
        with pytest.raises(ExecutionInterrupted):
            await shell.arun('kept = []\nready.set()\ntry:\n    await asyncio.sleep(30)\nfinally:\n    kept.append("cleaned")')
        assert asyncio.current_task().cancelling() == 0
        assert (await shell.arun('kept')).result == ['cleaned']
        assert get_run_ctx() is None

    task = asyncio.create_task(run())
    await ready.wait()
    if from_thread:
        assert await asyncio.to_thread(shell.interrupt)
    else:
        assert shell.interrupt()
    await asyncio.wait_for(task, 2)
    assert shell.interrupt() is False


async def test_external_cancellation_remains_cancelled_error():
    shell = Shell()
    shell.namespace['asyncio'] = asyncio
    task = asyncio.create_task(shell.arun('kept = 42\nawait asyncio.sleep(30)'))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert shell.run('kept').result == 42


def test_cross_thread_sync_interrupt_refused():
    shell = Shell()
    ready, done = threading.Event(), threading.Event()
    shell.namespace.update(ready=ready, done=done)
    worker = threading.Thread(target=shell.run, args=('ready.set()\ndone.wait(2)',))
    worker.start()
    try:
        assert ready.wait(2)
        with pytest.raises(RuntimeError, match='owning thread'):
            shell.interrupt()
    finally:
        done.set()
        worker.join(2)
    assert not worker.is_alive()


async def test_suppressed_interrupt_does_not_leave_host_task_cancelling():
    shell = Shell()
    ready = asyncio.Event()
    shell.namespace.update(asyncio=asyncio, ready=ready)

    async def run():
        response = await shell.arun('ready.set()\ntry:\n    await asyncio.sleep(30)\nexcept asyncio.CancelledError:\n    kept = 42')
        assert response.exception is None
        assert asyncio.current_task().cancelling() == 0
        assert (await shell.arun('kept')).result == 42

    task = asyncio.create_task(run())
    await ready.wait()
    assert shell.interrupt()
    await asyncio.wait_for(task, 2)
