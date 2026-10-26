"""Run embedded/CLI examples directly from a built wheel, without installing it."""
import asyncio
import importlib.metadata
import inspect
from pathlib import Path
import sys

wheel = Path(sys.argv[1]).resolve()
assert wheel.is_file() and wheel.suffix == '.whl'
sys.path.insert(0, str(wheel))

import pynteract
from pynteract import ExecutionInterrupted, Shell

assert str(wheel) in pynteract.__file__, pynteract.__file__
assert importlib.metadata.version('pynteract') == '1.2.0'
with Shell(display_mode='none') as shell:
    response = shell.run('x = 41 + 1\nx')
    assert response.exception is None and response.result == 42
    assert response.stdout == response.stderr == ''
    assert shell.run('%timeit -n 1 -r 1 print("hello")').stdout == 'hello\n'
    assert shell.run('raise ValueError("wheel-error")', silent=True).exception is not None
    shell.run('def example():\n    return 42')
    assert 'return 42' in inspect.getsource(shell.namespace['example'])
    assert shell.interrupt() is False
    shell.namespace['stop'] = shell.interrupt
    try:
        shell.run('kept = 42\nstop()')
    except ExecutionInterrupted:
        pass
    else:
        raise AssertionError('Expected ExecutionInterrupted from the built wheel')
    assert shell.run('kept').result == 42

async def async_example():
    with Shell(display_mode='none') as shell:
        response = await shell.arun('import asyncio\nawait asyncio.sleep(0)\n42')
        assert response.exception is None and response.result == 42

asyncio.run(async_example())
with Shell(capture_limit=3, hook_buffer=False, stdout_hook=lambda *_: None) as shell:
    response = shell.run("print('abcdef')")
    assert response.stdout.startswith('abc\n[output truncated:')
    assert response.output_stats['stdout'] == {'written_chars': 7, 'truncated': True}

entry = next(ep for ep in importlib.metadata.distribution('pynteract').entry_points
             if ep.group == 'console_scripts' and ep.name == 'pynteract')
assert entry.load()(['--version']) == 0
assert entry.load()(['--no-interact']) == 0
print('Wheel embedding, async, introspection, magics and CLI smoke checks passed.')
