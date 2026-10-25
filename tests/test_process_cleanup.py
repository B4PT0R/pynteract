import io
import shutil
import sys
import threading

import pytest

from pynteract import Shell
from pynteract import processes


@pytest.mark.parametrize('pipe', ['stdout', 'stderr'])
def test_reader_failure_propagates_and_reaps_process(monkeypatch, pipe):
    children = []
    popen = processes.subprocess.Popen

    def record(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    failures = []

    def fail(data, buffer):
        if not failures:
            failures.append(data)
            raise RuntimeError('output delivery failed')

    monkeypatch.setattr(processes.subprocess, 'Popen', record)
    shell = Shell(display_mode='none', **{pipe + '_hook': fail})
    command = [sys.executable, '-c',
               f'import sys,time; print("child-output", file=sys.{pipe}, flush=True); time.sleep(30)']
    baseline = set(threading.enumerate())
    from pynteract.collector import Collector
    with Collector(shell) as collector:
        shell._run_process(command)
    assert isinstance(collector.exception, RuntimeError)
    assert str(collector.exception) == 'output delivery failed'
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed
    assert set(threading.enumerate()) == baseline


def test_thread_setup_failure_also_reaps_child(monkeypatch):
    children = []
    popen = processes.subprocess.Popen

    def record(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def fail(thread):
        raise KeyboardInterrupt()

    monkeypatch.setattr(processes.subprocess, 'Popen', record)
    with pytest.raises(KeyboardInterrupt):
        processes.run_streamed([sys.executable, '-c', 'import time; time.sleep(30)'], prepare_thread=fail)
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


@pytest.mark.skipif(not shutil.which('bash'), reason='bash required')
def test_bash_reader_failure_is_a_cell_error():
    def fail(data, buffer):
        raise RuntimeError('bash delivery failed')

    shell = Shell(stdout_hook=fail, stderr_hook=lambda *_: None)
    response = shell.run('%%bash\necho child-output\nsleep 30')
    assert isinstance(response.exception, RuntimeError)
    assert str(response.exception) == 'bash delivery failed'
    assert 'child-output' in response.stdout


def test_both_pipes_are_drained_without_deadlock():
    output = io.StringIO()
    shell = Shell(stderr_hook=lambda *_: None)
    from pynteract.collector import Collector
    with Collector(shell) as collector:
        code = 'import sys; sys.stdout.write("x"*200000); sys.stderr.write("y"*200000)'
        assert shell._run_process([sys.executable, '-c', code], stdout=output) == 0
    assert output.getvalue() == 'x' * 200000
    assert collector.get_stderr() == 'y' * 200000
