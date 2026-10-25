import asyncio

import pytest

from pynteract import Shell


@pytest.mark.parametrize('tail', ['return 7', 'break', 'await missing()', 'from __future__ import annotations'])
def test_invalid_cell_has_no_partial_execution(tail):
    shell = Shell(silent=True)
    response = shell.run('side_effect = 123\n' + tail)
    assert isinstance(response.exception, SyntaxError)
    assert 'side_effect' not in shell.namespace


def test_failed_cell_does_not_commit_future_flags():
    shell = Shell(silent=True)
    response = shell.run('from __future__ import annotations\nreturn 7')
    assert isinstance(response.exception, SyntaxError)
    assert shell._futures.flags == 0
    assert shell.run('def f(x: int): pass\nf.__annotations__').result == {'x': int}


@pytest.mark.asyncio
async def test_async_invalid_cell_has_no_partial_execution():
    shell = Shell(silent=True)
    response = await shell.arun('side_effect = 123\nawait missing()\nreturn 7')
    assert isinstance(response.exception, SyntaxError)
    assert 'side_effect' not in shell.namespace


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['reset', 'set', 'update'])
async def test_namespace_mutation_from_competing_task_or_thread_is_rejected(operation):
    shell = Shell(silent=True)
    entered, release = asyncio.Event(), asyncio.Event()
    shell.update_namespace(entered=entered, release=release, x=42)
    task = asyncio.create_task(shell.arun('entered.set()\nawait release.wait()\nx'))
    await entered.wait()
    action = {'reset': shell.reset_namespace,
              'set': lambda: shell.set_namespace({}),
              'update': lambda: shell.update_namespace(x=7)}[operation]
    try:
        with pytest.raises(RuntimeError, match='Cannot mutate'):
            action()
        with pytest.raises(RuntimeError, match='Cannot mutate'):
            await asyncio.to_thread(action)
    finally:
        release.set()
    assert (await task).result == 42
    action()  # Lock remains usable after the cell finishes.


def test_inline_namespace_reset_remains_intentional_and_supported():
    shell = Shell(silent=True)
    response = shell.run('x = 1\n__shell__.restart_session(rerun_startup=False)\n42')
    assert response.exception is None
    assert response.result == 42
    assert 'x' not in shell.namespace
