import asyncio
import gc
import inspect
import linecache
import sys
import types
import weakref

import pytest

from pynteract import Shell
from pynteract.cli import main


def test_close_releases_modules_and_shell_without_clearing_user_data():
    shell = Shell(silent=True)
    namespace = shell.namespace
    name = namespace['__name__']
    shell.run('answer = 42')
    ref = weakref.ref(shell)
    shell.close()
    shell.close()
    assert shell.closed
    assert name not in sys.modules
    assert namespace['answer'] == 42
    del namespace, shell
    gc.collect()
    assert ref() is None


def test_context_manager_closes_on_exception():
    with pytest.raises(ValueError), Shell() as shell:
        name = shell.namespace['__name__']
        raise ValueError('host error')
    assert shell.closed
    assert name not in sys.modules


def test_closed_shell_refuses_execution_and_mutations():
    shell = Shell()
    shell.close()
    for operation in (lambda: shell.run('pass'), shell.reset_namespace,
                      lambda: shell.set_namespace({}), lambda: shell.update_namespace(x=1),
                      shell.__enter__):
        with pytest.raises(RuntimeError, match='closed'):
            operation()


async def test_closed_shell_refuses_async_execution():
    shell = Shell()
    shell.close()
    with pytest.raises(RuntimeError, match='closed'):
        await shell.arun('pass')


def test_close_is_rejected_during_execution_even_in_same_task():
    with Shell(silent=True) as shell:
        response = shell.run('__shell__.close()')
        assert isinstance(response.exception, RuntimeError)
        assert not shell.closed
        assert shell.run('42').result == 42


def test_close_preserves_borrowed_module(monkeypatch):
    module = types.ModuleType('lifecycle_borrowed')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with Shell(namespace=module.__dict__) as shell:
        assert shell.namespace is module.__dict__
    assert sys.modules[module.__name__] is module


def test_close_restores_replaced_module_and_releases_external_globals(monkeypatch):
    name = 'lifecycle_replaced'
    original = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, original)
    with Shell(module_name=name, silent=True) as shell:
        external = {}
        shell.run('x = 1', globals=external)
        external_name = external['__name__']
        assert sys.modules[name] is not original
        shell.set_namespace({'x': 2})
        shell.reset_namespace()
    assert sys.modules[name] is original
    assert external_name not in sys.modules
    assert external['x'] == 1


def test_close_does_not_remove_host_replacement(monkeypatch):
    with Shell() as shell:
        name = shell.namespace['__name__']
        replacement = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, replacement)
    assert sys.modules[name] is replacement
    # monkeypatch would restore the already-closed shell module otherwise.
    monkeypatch.undo()
    sys.modules.pop(name, None)


def test_out_of_order_close_does_not_resurrect_closed_shell(monkeypatch):
    name = 'lifecycle_overlapping'
    original = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, original)
    first, second = Shell(module_name=name), Shell(module_name=name)
    second_module = sys.modules[name]
    first.close()
    assert sys.modules[name] is second_module
    second.close()
    assert sys.modules[name] is original


def test_introspection_survives_history_eviction_and_reused_filenames():
    source = 'def demo():\n    return 42\n'
    with Shell(silent=True, history_size=1) as shell:
        shell.run(source, filename='<same-cell>')
        demo = shell.namespace['demo']
        internal = demo.__code__.co_filename
        shell.run('def other():\n    return 99\n', filename='<same-cell>')
        shell.run('0')
        assert inspect.getsource(demo) == source
        assert inspect.getsource(shell.namespace['other']) == 'def other():\n    return 99\n'
        assert internal in linecache.cache
    assert internal not in linecache.cache


def test_source_caches_are_independent_across_shells():
    first, second = Shell(silent=True), Shell(silent=True)
    try:
        first.run('def demo():\n    return 1\n', filename='<shared-cell>')
        second.run('def demo():\n    return 2\n', filename='<shared-cell>')
        first.close()
        assert inspect.getsource(second.namespace['demo']) == 'def demo():\n    return 2\n'
    finally:
        first.close()
        second.close()


def test_source_cache_does_not_overwrite_real_files_or_host_cache(tmp_path, monkeypatch):
    path = tmp_path / 'existing.py'
    path.write_text('host = 1\n')
    host_entry = (9, None, ['host = 1\n'], str(path))
    monkeypatch.setitem(linecache.cache, str(path), host_entry)
    source = 'def demo():\n    return 42\n'
    with Shell(silent=True) as shell:
        shell.run(source, filename=str(path))
        assert inspect.getsource(shell.namespace['demo']) == source
        assert linecache.cache[str(path)] is host_entry
        response = shell.run('1 / 0', filename=str(path))
        assert response.exception.enriched_traceback['frames'][0]['filename'] == str(path)
        assert '=> 1 | 1 / 0' in response.stderr
    assert path.read_text() == 'host = 1\n'
    assert linecache.cache[str(path)] is host_entry


def test_close_preserves_host_replacement_of_owned_cache():
    shell = Shell(silent=True)
    shell.run('def demo():\n    pass\n')
    name = shell.namespace['demo'].__code__.co_filename
    replacement = (5, None, ['host\n'], name)
    try:
        linecache.cache[name] = replacement
        shell.close()
        assert linecache.cache[name] is replacement
    finally:
        linecache.cache.pop(name, None)


def test_cli_closes_its_shell_on_script_exit(tmp_path, monkeypatch):
    script = tmp_path / 'exit.py'
    script.write_text('raise SystemExit(7)\n')
    closed = []
    close = Shell.close

    def record(shell):
        close(shell)
        closed.append(shell.closed)

    monkeypatch.setattr(Shell, 'close', record)
    assert main([str(script)]) == 7
    assert closed == [True]


@pytest.mark.parametrize('close_first', [True, False])
def test_swapping_overlapping_shells_does_not_resurrect_old_modules(monkeypatch, close_first):
    name = 'lifecycle_swapped_overlap'
    original = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, original)
    first, second = Shell(module_name=name), Shell(module_name=name)
    first.set_namespace({})
    order = (first, second) if close_first else (second, first)
    for shell in order:
        shell.close()
    assert sys.modules[name] is original


async def test_close_is_rejected_from_other_task_during_execution():
    started, finish = asyncio.Event(), asyncio.Event()
    with Shell(silent=True, namespace={'started': started, 'finish': finish}) as shell:
        task = asyncio.create_task(shell.arun('started.set()\nawait finish.wait()'))
        try:
            await started.wait()
            with pytest.raises(RuntimeError, match='executing'):
                shell.close()
        finally:
            finish.set()
            await task
        assert not shell.closed
