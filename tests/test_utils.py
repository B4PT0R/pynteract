"""Tests de non-régression pour utils.py."""
import sys
import threading
import pytest
from pynteract.utils import (
    content_hash,
    short_id,
    debug_print,
    Thread,
    register_thread_context_hook,
    _ID_ALPHABET,
)


# ── content_hash ──────────────────────────────────────────────────────────────

class TestContentHash:
    def test_returns_hex_string(self):
        h = content_hash("hello")
        assert isinstance(h, str)
        assert all(c in "0123456789abcdef" for c in h)

    def test_sha256_length(self):
        assert len(content_hash("x")) == 64

    def test_deterministic(self):
        assert content_hash("abc") == content_hash("abc")

    def test_different_inputs_differ(self):
        assert content_hash("a") != content_hash("b")

    def test_empty_string(self):
        h = content_hash("")
        assert len(h) == 64


# ── short_id ──────────────────────────────────────────────────────────────────

class TestShortId:
    def test_default_length(self):
        assert len(short_id()) == 10

    def test_custom_length(self):
        assert len(short_id(16)) == 16

    def test_alphabet(self):
        for _ in range(50):
            assert all(c in _ID_ALPHABET for c in short_id())

    def test_uniqueness(self):
        ids = {short_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_no_special_chars(self):
        for _ in range(50):
            sid = short_id()
            assert sid.isalnum()


# ── debug_print ───────────────────────────────────────────────────────────────

class TestDebugPrint:
    def test_writes_to_real_stdout(self, capsys):
        # debug_print bypasses sys.stdout patching — writes to sys.__stdout__
        # which pytest captures via its low-level fd capture.
        debug_print("test_debug")
        # We can't easily capture sys.__stdout__ in pytest without fd-level
        # capture, so we just assert it doesn't raise.

    def test_multiple_args(self):
        debug_print("a", "b", "c", sep="-")  # must not raise

    def test_flush(self):
        debug_print("flush test", flush=True)  # must not raise


# ── Thread + register_thread_context_hook ─────────────────────────────────────

class TestThread:
    def teardown_method(self, method):
        # Always reset the hook after each test
        register_thread_context_hook(None)

    def test_returns_thread(self):
        t = Thread(target=lambda: None)
        assert isinstance(t, threading.Thread)

    def test_thread_runs(self):
        results = []
        t = Thread(target=lambda: results.append(42))
        t.start()
        t.join()
        assert results == [42]

    def test_hook_called_on_thread_creation(self):
        hooked = []
        register_thread_context_hook(lambda t: hooked.append(t))
        t = Thread(target=lambda: None)
        assert len(hooked) == 1
        assert hooked[0] is t

    def test_hook_not_called_after_unregister(self):
        hooked = []
        register_thread_context_hook(lambda t: hooked.append(t))
        register_thread_context_hook(None)
        Thread(target=lambda: None)
        assert hooked == []

    def test_hook_receives_correct_thread(self):
        received = []
        register_thread_context_hook(lambda t: received.append(id(t)))
        t = Thread(target=lambda: None)
        assert received[0] == id(t)

    def test_no_streamlit_import_without_hook(self):
        # Streamlit must NOT be imported as a side effect of creating a thread
        before = set(sys.modules.keys())
        Thread(target=lambda: None)
        after = set(sys.modules.keys())
        new_modules = after - before
        assert not any("streamlit" in m for m in new_modules)
