from pynteract import Shell


def test_cyclic_causes_preserve_original_error():
    shell = Shell(silent=True)
    response = shell.run('''
a = ValueError('first')
b = TypeError('second')
a.__cause__ = b
b.__cause__ = a
raise a
''')
    assert response.exception is shell.namespace['a']
    assert 'ValueError: first' in response.stderr
    assert 'TypeError: second' in response.stderr
    assert 'RecursionError' not in response.stderr
    assert len(response.exception.enriched_traceback['exceptions']) == 2


def test_nested_exception_group_has_text_and_structured_children():
    shell = Shell(silent=True)
    response = shell.run('''
raise ExceptionGroup('group', [ValueError('leaf-one'),
    ExceptionGroup('nested', [TypeError('leaf-two')])])
''')
    assert isinstance(response.exception, ExceptionGroup)
    assert 'leaf-one' in response.stderr
    assert 'leaf-two' in response.stderr
    tree = response.exception.enriched_traceback['exceptions'][-1]
    assert len(tree['children']) == 2
    assert len(tree['children'][1]['exceptions'][0]['children']) == 1


def test_group_leaf_cause_can_point_back_to_group():
    shell = Shell(silent=True)
    response = shell.run('''
leaf = ValueError('leaf')
group = ExceptionGroup('group', [leaf])
leaf.__cause__ = group
raise group
''')
    assert response.exception is shell.namespace['group']
    assert 'ValueError: leaf' in response.stderr
    assert 'RecursionError' not in response.stderr
