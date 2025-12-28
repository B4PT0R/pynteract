from __future__ import annotations

import io

from pynteract import Shell
from pynteract.streams import StdIOProxy


def test_stream_exposes_encoding_and_isatty_like_real_stdout():
    shell = Shell(display_mode="none", stdout_hook=lambda *_a, **_k: None)
    resp = shell.run(
        "import sys\n"
        "print(isinstance(sys.stdout.encoding, str))\n"
        "print(isinstance(sys.stdout.isatty(), bool))\n"
    )
    assert resp.exception is None
    assert "True" in (resp.stdout or "")


def test_stream_buffer_accepts_bytes_and_is_captured():
    shell = Shell(display_mode="none", stdout_hook=lambda *_a, **_k: None)
    resp = shell.run(
        "import sys\n"
        "sys.stdout.buffer.write(b'abc')\n"
        "sys.stdout.buffer.write(b'\\n')\n"
        "sys.stdout.flush()\n"
    )
    assert resp.exception is None
    assert (resp.stdout or "").endswith("abc\n")


def test_stream_fileno_delegates_when_available():
    shell = Shell(display_mode="none", stdout_hook=lambda *_a, **_k: None)
    resp = shell.run(
        "import sys\n"
        "try:\n"
        "    v = sys.stdout.fileno()\n"
        "    print(isinstance(v, int))\n"
        "except Exception:\n"
        "    print('no')\n"
    )
    assert resp.exception is None
    assert "True" in (resp.stdout or "")


def test_user_code_can_redirect_sys_stdout_without_breaking_shell_capture():
    shell = Shell(display_mode="none", stdout_hook=lambda *_a, **_k: None)
    resp = shell.run(
        "import io\n"
        "import sys\n"
        "import contextlib\n"
        "\n"
        "print('a')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    print('b')\n"
        "print('c')\n"
        "\n"
        "captured_b = buf.getvalue()\n",
    )
    assert resp.exception is None
    assert (resp.stdout or "") == "a\nc\n"
    assert shell.namespace["captured_b"] == "b\n"


def test_stdio_proxy_flush_noops_when_delegate_closed():
    delegate = io.StringIO()
    proxy = StdIOProxy(delegate, which="stdout")
    delegate.close()
    proxy.flush()
