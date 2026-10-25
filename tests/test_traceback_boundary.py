import traceback

import pytest

from pynteract import Shell


def frames(response):
    return response.exception.enriched_traceback['frames']


def test_cell_boundary_preserves_library_frames_and_original_traceback():
    # Even an identical filename is not evidence that host code is a cell.
    filename = __file__

    def library_call():
        raise ValueError('library failure')

    shell = Shell(silent=True, namespace={'library_call': library_call})
    response = shell.run('library_call()', filename=filename)
    assert [f['name'] for f in frames(response)] == ['<module>', 'library_call']
    assert frames(response)[0]['lineno'] == 1
    assert response.exception.enriched_traceback['skipped_frames'] == 1
    assert traceback.extract_tb(response.exception.__traceback__)[0].name == 'run'


@pytest.mark.parametrize('code', ['if :', 'x = 1\nreturn', 'from __future__ import missing_feature'])
def test_compilation_errors_have_source_diagnostics_not_engine_stack(code):
    response = Shell(silent=True).run(code, filename='<invalid-cell>')
    assert isinstance(response.exception, SyntaxError)
    assert frames(response) == []
    assert 'shell.py' not in response.stderr
    assert 'asttokens' not in response.stderr
    assert 'SyntaxError' in response.stderr
    if code != 'from __future__ import missing_feature':
        assert '<invalid-cell>' in response.stderr


async def test_async_boundary_and_downstream_frames():
    async def library_call():
        raise ValueError('async failure')

    response = await Shell(silent=True, namespace={'library_call': library_call}).arun(
        'await library_call()', filename='<async-cell>')
    assert [f['name'] for f in frames(response)] == ['<module>', 'library_call']
    assert frames(response)[0]['filename'] == '<async-cell>'
    assert 'shell.py' not in response.stderr


def test_every_chained_exception_starts_in_cell():
    response = Shell(silent=True).run('''
try:
    1 / 0
except ZeroDivisionError as cause:
    raise ValueError('outer') from cause
''', filename='<chain-cell>')
    for part in response.exception.enriched_traceback['exceptions']:
        assert part['frames'][0]['filename'] == '<chain-cell>'
    assert 'shell.py' not in response.stderr


def test_exception_group_children_start_at_their_own_cell_frame():
    response = Shell(silent=True).run('''
try:
    1 / 0
except ZeroDivisionError as error:
    raise ExceptionGroup('group', [error]) from None
''', filename='<group-cell>')
    child = response.exception.enriched_traceback['exceptions'][-1]['children'][0]
    assert child['exceptions'][0]['frames'][0]['filename'] == '<group-cell>'
    assert 'shell.py' not in response.stderr


def test_nested_execution_has_independent_boundaries():
    shell = Shell(silent=True)
    outer = shell.run("inner = __shell__.run('1 / 0', filename='<inner>')", filename='<outer>')
    assert outer.exception is None
    inner = shell.namespace['inner']
    assert [f['filename'] for f in frames(inner)] == ['<inner>']


def test_function_survives_history_eviction_and_keeps_cell_identity():
    shell = Shell(silent=True, history_size=1)
    shell.run("def fail():\n    raise ValueError('old cell')", filename='<old>')
    shell.run('0')
    response = shell.run('fail()', filename='<new>')
    assert [f['filename'] for f in frames(response)] == ['<new>', '<old>']


def test_runtime_syntax_error_retains_real_calls():
    def library_call():
        compile('if :', '<generated>', 'exec')

    response = Shell(silent=True, namespace={'library_call': library_call}).run('library_call()')
    assert [f['name'] for f in frames(response)] == ['<module>', 'library_call']
    assert '<generated>' in response.stderr


def test_failing_engine_hook_remains_diagnosable():
    def failing_hook(node, source):
        raise RuntimeError('hook failure')

    response = Shell(silent=True, pre_execute_hook=failing_hook).run('1')
    assert frames(response)[-1]['name'] == 'failing_hook'
    assert 'RuntimeError: hook failure' in response.stderr
