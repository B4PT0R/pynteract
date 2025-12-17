from __future__ import annotations

from pynteract import Shell


def test_history_keeps_last_n_entries_and_filenames_increment():
    shell = Shell(display_mode="none", history_size=2)
    r1 = shell.run("1")
    r2 = shell.run("2")
    r3 = shell.run("3")
    assert r1.exception is None and r2.exception is None and r3.exception is None

    keys = list(shell.history.keys())
    assert len(keys) == 2
    assert keys[0].startswith("<shell-input-")
    assert keys[1].startswith("<shell-input-")
    assert "1" not in keys[0]  # older entry should have been evicted
    assert shell.history[keys[0]].result == 2
    assert shell.history[keys[1]].result == 3


def test_custom_filename_is_used_for_tracebacks():
    shell = Shell(display_mode="none")
    resp = shell.run("1/0", filename="<mycell>")
    assert isinstance(resp.exception, Exception)
    assert "<mycell>" in getattr(resp.exception, "enriched_traceback_string", "")


def test_traceback_includes_chaining_message():
    shell = Shell(display_mode="none")
    resp = shell.run(
        "try:\n"
        "    1/0\n"
        "except ZeroDivisionError as e:\n"
        "    raise ValueError('boom') from e\n"
    )
    assert isinstance(resp.exception, Exception)
    text = getattr(resp.exception, "enriched_traceback_string", "")
    assert "direct cause" in text
