"""Parsing preserves trusted Python expressions and opaque magic bodies."""
import pytest

from pynteract import Shell
from pynteract.magics import MagicParser


@pytest.mark.parametrize(('source', 'expected'), [
    ("{ {'outer': {'inner': 7}}['outer']['inner'] }", '7'),
    ('{ "}" }', '}'),
    ("{ '''a}b''' }", 'a}b'),
    ('{ "a\\\"}b" }', 'a"}b'),
    ('{{literal}} { {1, 2} == {2, 1} }', '{literal} True'),
    ('{ (\n {"x": 4}\n)["x"] }', '4'),
    ('{ f"value={3}" }', 'value=3'),
])
def test_template_python_braces(source, expected):
    assert MagicParser._render_template(source, {}, {}) == expected


def test_templates_can_call_namespace_objects_not_a_sandbox():
    calls = []
    assert MagicParser._render_template('{call()}', {'call': lambda: calls.append(1)}, {}) == 'None'
    assert calls == [1]


@pytest.mark.parametrize('source', ['%echo\tword', 'value = %echo\tword'])
def test_tab_separated_line_magic(source):
    with Shell(display_mode='none') as shell:
        shell.register_magic(name='echo', mode='line')(lambda text: text)
        response = shell.run(source)
        assert response.exception is None
        assert (shell.namespace['value'] if source.startswith('value') else response.result) == 'word'


def test_cell_body_is_not_parsed_as_python_or_shell_escape():
    body = '!echo hello\n%not_a_magic\n!!literal\n'
    with Shell(display_mode='none') as shell:
        shell.register_magic(name='raw', mode='cell')(lambda text: text)
        response = shell.run('%%raw\n' + body)
        assert response.exception is None
        assert response.result == body


@pytest.mark.parametrize('options', ['-n 1 -r 1', '-r\t1\t-n\t1'])
def test_timeit_preserves_python_quotes_and_spacing(options):
    with Shell(display_mode='none') as shell:
        response = shell.run(f'%timeit {options} print("hello  world", "-n")')
        assert response.exception is None
        assert response.stdout == 'hello  world -n\n'
        assert response.result.startswith('Best of 1:')


@pytest.mark.parametrize('options', ['-n 0', '-r -1', '-n', '-r nope'])
def test_timeit_rejects_invalid_counts(options):
    with Shell(display_mode='none') as shell:
        response = shell.run('%timeit ' + options)
        assert isinstance(response.exception, ValueError)
