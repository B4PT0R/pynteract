"""Startup and terminal tests without reading personal config or opening a TTY."""
from types import SimpleNamespace

import pytest

from pynteract import Shell
from pynteract import ptk, user_config
from pynteract.cli import main


@pytest.fixture
def startup(tmp_path, monkeypatch):
    monkeypatch.setenv('PYNTERACT_CONFIG_DIR', str(tmp_path))
    return tmp_path / 'startup.py'


@pytest.mark.parametrize('content', [None, '', '# nothing to execute\n'])
def test_optional_startup_is_cached(startup, content, capsys):
    if content is not None:
        startup.write_text(content)
    with Shell(display_mode='none') as shell:
        assert shell._run_startup(announce=False) == 0
        startup.write_text('raise AssertionError("must not reread")')
        assert shell._run_startup(announce=True) == 0
    assert capsys.readouterr().err == ''


def test_startup_encoding_once_and_restart(startup, capsys):
    startup.write_bytes('# coding: latin-1\nname = "caf\xe9"\n'.encode('latin-1'))
    with Shell(display_mode='none') as shell:
        assert shell._run_startup(announce=False) == 0
        assert shell.namespace['name'] == 'caf\xe9'
        startup.write_text('name = "restarted"')
        assert shell._run_startup(announce=True) == 0
        assert shell.namespace['name'] == 'caf\xe9'
        assert capsys.readouterr().out == 'Running startup...\nReady!\n'
        shell.update_namespace(exit=lambda: None, transient=1)
        exit_fn = shell.namespace['exit']
        assert shell.restart_session(announce=False) == 0
        assert shell.namespace['name'] == 'restarted'
        assert 'transient' not in shell.namespace
        assert shell.namespace['exit'] is exit_fn


def test_startup_failure_does_not_repeat_announced_error(startup, capsys):
    startup.write_text('raise ValueError("startup failed")')
    with Shell(display_mode='none') as shell:
        assert shell._run_startup(announce=True) == 1
        assert 'startup failed' in capsys.readouterr().err
        assert shell._run_startup(announce=True) == 1
        assert capsys.readouterr().err == ''


@pytest.mark.parametrize('kind', ['invalid_encoding', 'unwritable_config'])
def test_unreadable_optional_startup_warns_once(startup, monkeypatch, capsys, kind):
    if kind == 'invalid_encoding':
        startup.write_bytes(b'not_valid = "\xff"')
    else:
        def fail():
            raise PermissionError('config denied')
        monkeypatch.setattr(user_config, 'startup_path', fail)
    with Shell(display_mode='none') as shell:
        assert shell._run_startup(announce=True) == 0
        assert 'could not read startup' in capsys.readouterr().err
        assert shell._run_startup(announce=True) == 0
        assert capsys.readouterr().err == ''


def test_cli_interactive_startup_runs_before_script_once(startup, tmp_path, monkeypatch):
    startup.write_text('events = ["startup"]')
    script = tmp_path / 'script.py'
    script.write_text('events.append("script")')
    observed = []
    def interact(shell):
        assert shell._run_startup(announce=True) == 0
        observed.extend(shell.namespace['events'])
        return 0
    monkeypatch.setattr(Shell, 'interact', interact)
    assert main(['-i', str(script)]) == 0
    assert observed == ['startup', 'script']


def test_interact_ctrl_c_eof_and_execution(startup, monkeypatch, capsys):
    from pynteract import shell as module
    entries = iter([KeyboardInterrupt(), 'answer = 42', EOFError()])
    def prompt(*args, **kwargs):
        item = next(entries)
        if isinstance(item, BaseException):
            raise item
        return item
    monkeypatch.setattr(module, 'prompt', prompt)
    with Shell(display_mode='none') as shell:
        assert shell.interact() == 0
        assert shell.namespace['answer'] == 42
    assert 'KeyboardInterrupt' in capsys.readouterr().out


def test_interact_missing_terminal_dependency_fails_once(startup, monkeypatch, capsys):
    from pynteract import shell as module
    calls = []
    def prompt(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('install pynteract[terminal]')
    monkeypatch.setattr(module, 'prompt', prompt)
    with Shell(display_mode='none') as shell:
        assert shell.interact() == 1
    assert calls == [1]
    assert 'pynteract[terminal]' in capsys.readouterr().err


@pytest.fixture
def bindings(monkeypatch, tmp_path):
    pytest.importorskip('prompt_toolkit')
    monkeypatch.setattr(ptk, '_PTK_SHARED', None)
    monkeypatch.setattr(ptk, '_PTK_SESSIONS', {})
    monkeypatch.setenv('PYNTERACT_CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('PROMPT_TOOLKIT_NO_CPR', '1')
    return ptk._ensure_ptk_shared()


def test_terminal_bindings_are_registered_before_tab_and_stay_stable(bindings):
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.keys import Keys
    kb = bindings['key_bindings']
    for keys in [(Keys.BackTab,), (Keys.ControlC,), (Keys.ControlX,),
                 (Keys.ControlV,), (Keys.Enter,), (Keys.Backspace,),
                 (Keys.Escape, Keys.Enter)]:
        assert kb.get_bindings_for_keys(keys), keys
    count = len(kb.bindings)
    buffer = Buffer(multiline=True)
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer))
    kb.get_bindings_for_keys((Keys.Tab,))[-1].handler(event)
    assert buffer.text == '    '
    assert len(kb.bindings) == count
    kb.get_bindings_for_keys((Keys.BackTab,))[-1].handler(event)
    assert buffer.text == ''
    buffer.insert_text('if True:')
    kb.get_bindings_for_keys((Keys.Enter,))[-1].handler(event)
    assert buffer.text == 'if True:\n    '
    kb.get_bindings_for_keys((Keys.Backspace,))[-1].handler(event)
    assert buffer.text == 'if True:\n'


def test_terminal_copy_cut_and_paste_use_shared_clipboard(bindings):
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.selection import SelectionState
    from prompt_toolkit.clipboard import InMemoryClipboard
    kb = bindings['key_bindings']
    clipboard = InMemoryClipboard()
    buffer = Buffer(document=Document('hello', cursor_position=5))
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer, clipboard=clipboard))
    buffer.selection_state = SelectionState(original_cursor_position=0)
    kb.get_bindings_for_keys((Keys.ControlC,))[-1].handler(event)
    assert clipboard.get_data().text == 'hello'
    assert buffer.text == 'hello'
    buffer.selection_state = SelectionState(original_cursor_position=0)
    kb.get_bindings_for_keys((Keys.ControlX,))[-1].handler(event)
    assert buffer.text == ''
    assert clipboard.get_data().text == 'hello'
    kb.get_bindings_for_keys((Keys.ControlV,))[-1].handler(event)
    assert buffer.text == 'hello'


def test_terminal_history_falls_back_to_memory(monkeypatch, bindings):
    from prompt_toolkit.history import InMemoryHistory
    calls = []
    class FakeSession:
        def __init__(self, **kwargs):
            calls.append(kwargs)
    def denied(**kwargs):
        raise PermissionError('history denied')
    monkeypatch.setitem(bindings, 'PromptSession', FakeSession)
    monkeypatch.setattr(user_config, 'history_path', denied)
    session = ptk._get_ptk_session('text')
    assert isinstance(calls[0]['history'], InMemoryHistory)
    assert ptk._get_ptk_session('text') is session
    assert len(calls) == 1


def test_terminal_highlighting_validation():
    with pytest.raises(ValueError, match='highlighting'):
        ptk.prompt('>', highlighting='invalid')


def test_terminal_submit_binding(bindings):
    from prompt_toolkit.keys import Keys
    calls = []
    buffer = SimpleNamespace(validate_and_handle=lambda: calls.append('submit'))
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer))
    bindings['key_bindings'].get_bindings_for_keys((Keys.Escape, Keys.Enter))[-1].handler(event)
    assert calls == ['submit']


def test_missing_terminal_extra_has_actionable_error(monkeypatch):
    import builtins
    original_import = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == 'prompt_toolkit' or name.startswith('prompt_toolkit.'):
            raise ImportError('deliberately unavailable')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(ptk, '_PTK_SHARED', None)
    monkeypatch.setattr(builtins, '__import__', blocked)
    with pytest.raises(RuntimeError, match=r'pynteract\[terminal\]'):
        ptk._ensure_ptk_shared()
