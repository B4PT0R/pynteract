import sys

import pytest

from pynteract import Shell
from pynteract.cli import main


@pytest.mark.parametrize('interactive', [False, True])
def test_run_failure_propagates_and_streams_once(tmp_path, interactive):
    script = tmp_path / 'failure.py'
    script.write_text("import sys\nprint('child-out')\nsys.stderr.write('child-err\\n')\npartial = 3\n1 / 0\n")
    stdout, stderr = [], []
    shell = Shell(stdout_hook=lambda data, buffer: stdout.append(data),
                  stderr_hook=lambda data, buffer: stderr.append(data), display_mode='none')
    name = shell.namespace['__name__']
    option = '-i ' if interactive else ''
    response = shell.run(f"print('before', end=':')\n%run {option}{script}\nprint('unreachable')", filename='<outer>')
    assert isinstance(response.exception, ZeroDivisionError)
    assert response.stdout == 'before:child-out\n'
    assert ''.join(stdout) == response.stdout
    assert ''.join(stderr) == response.stderr
    assert response.stderr.count('ZeroDivisionError:') == 1
    assert 'child-err' in response.stderr
    assert shell.namespace['partial'] == 3
    assert shell.namespace['__name__'] == name
    assert shell.namespace['__file__'] == '<outer>'
    assert response.exception.enriched_traceback['frames'][0]['filename'] == str(script)


async def test_run_inside_arun_inherits_owned_hook_queue(tmp_path):
    script = tmp_path / 'async_parent.py'
    script.write_text("print('nested')\n")
    chunks = []

    async def stdout(data, buffer):
        chunks.append(data)

    response = await Shell(stdout_hook=stdout, display_mode='none').arun(f'%run {script}')
    assert response.exception is None
    assert response.stdout == 'nested\n'
    assert chunks == ['nested\n']


def test_silent_run_keeps_nested_output_without_hooks(tmp_path):
    script = tmp_path / 'silent.py'
    script.write_text("print('nested')\n")
    chunks = []
    response = Shell(silent=True, stdout_hook=lambda *args: chunks.append(args)).run(f'%run {script}')
    assert response.exception is None
    assert response.stdout == 'nested\n'
    assert not chunks


@pytest.mark.parametrize('runner', ['run', 'run_i', 'cli'])
def test_script_encoding_and_sibling_import(tmp_path, runner):
    module = 'pynteract_script_test_sibling'
    (tmp_path / f'{module}.py').write_text('value = 42\n')
    script = tmp_path / 'encoded.py'
    script.write_bytes(f"# coding: latin-1\nimport {module}\nassert {module}.value == 42\ntext = 'café'\nassert __name__ == '__main__'\n".encode('latin-1'))
    old_path, saved_path, old_argv, old_main = sys.path, sys.path[:], sys.argv, sys.modules['__main__']
    try:
        if runner == 'cli':
            assert main(['--no-interact', str(script)]) == 0
        else:
            shell = Shell(silent=True)
            response = shell.run(f"%run {'-i ' if runner == 'run_i' else ''}{script}")
            assert response.exception is None
            assert shell.namespace['text'] == 'café'
        assert sys.path is old_path and sys.path == saved_path
        assert sys.argv is old_argv
        assert sys.modules['__main__'] is old_main
    finally:
        sys.modules.pop(module, None)


@pytest.mark.parametrize(('expression', 'exit_code'), [('None', 0), ('7', 7), ("'bye'", 1)])
def test_cli_system_exit_restores_host_state(tmp_path, capsys, expression, exit_code):
    script = tmp_path / 'exit.py'
    script.write_text(f'import sys\nsys.argv = ["changed"]\nsys.path = []\nsys.exit({expression})\n')
    old_path, saved_path, old_argv, old_main = sys.path, sys.path[:], sys.argv, sys.modules['__main__']
    assert main(['--no-interact', str(script)]) == exit_code
    assert sys.path is old_path and sys.path == saved_path
    assert sys.argv is old_argv
    assert sys.modules['__main__'] is old_main
    if expression == "'bye'":
        assert 'bye' in capsys.readouterr().err


@pytest.mark.parametrize('runner', ['run', 'cli'])
def test_keyboard_interrupt_restores_script_state(tmp_path, runner):
    script = tmp_path / 'interrupt.py'
    script.write_text('raise KeyboardInterrupt\n')
    shell = Shell(silent=True)
    old_path, saved_path, old_argv, old_main = sys.path, sys.path[:], sys.argv, sys.modules['__main__']
    with pytest.raises(KeyboardInterrupt):
        if runner == 'cli':
            main(['--no-interact', str(script)])
        else:
            shell.run(f'%run {script}')
    assert sys.path is old_path and sys.path == saved_path
    assert sys.argv is old_argv
    assert sys.modules['__main__'] is old_main
    assert shell._execution_depth == 0


def test_cli_interactive_keeps_script_state_only_until_exit(tmp_path, monkeypatch):
    script = tmp_path / 'interactive.py'
    script.write_text('x = 99\n')
    old_argv, old_main = sys.argv, sys.modules['__main__']
    monkeypatch.setattr(Shell, '_run_startup', lambda self, **kw: 0)

    def interact(shell):
        assert shell.namespace['x'] == 99
        assert sys.modules['__main__'].__dict__ is shell.namespace
        assert sys.argv == [str(script)]
        return 4

    monkeypatch.setattr(Shell, 'interact', interact)
    assert main(['-i', str(script)]) == 4
    assert sys.argv is old_argv
    assert sys.modules['__main__'] is old_main


def test_repeated_exception_is_not_suppressed_in_later_run(tmp_path):
    script = tmp_path / 'repeat.py'
    script.write_text('raise shared\n')
    shell = Shell(silent=True)
    shell.namespace['shared'] = ValueError('again')
    first = shell.run(f'%run -i {script}')
    second = shell.run(f'%run -i {script}')
    assert first.stderr.count('ValueError: again') == 1
    assert second.stderr.count('ValueError: again') == 1
