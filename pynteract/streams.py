import io
from typing import Optional, Callable
from collections import deque
import sys
from .ptk import prompt
from .context import CAPTURE_STREAMS, RUN_CONTEXT, SILENT_STDIO, PENDING_STDIN_PROMPT

def stdout_write(data: str, buffer: str, ctx) -> None:
    """Default stdout hook that mirrors captured output to the real stdout."""
    if isinstance(sys.stdout, (Stream, StdIOProxy)):
        sys.__stdout__.write(data)
        sys.__stdout__.flush()
    else:
        sys.stdout.write(data)
        sys.stdout.flush()

def stderr_write(data: str, buffer: str, ctx) -> None:
    """Default stderr hook that mirrors captured output to the real stderr."""
    if isinstance(sys.stderr, (Stream, StdIOProxy)):
        sys.__stderr__.write(data)
        sys.__stderr__.flush()
    else:
        sys.stderr.write(data)
        sys.stderr.flush()

def stdin_readline(ctx) -> str:
    """Fallback stdin hook that reads from the console."""
    if isinstance(sys.stdin, StdinProxy):
        try:
            if PENDING_STDIN_PROMPT.get():
                # builtin input(prompt) prints the prompt to stdout before reading stdin.
                # We keep that prompt, but start prompt_toolkit on a fresh line.
                try:
                    out = sys.__stdout__
                    out.write("\n")
                    out.flush()
                except Exception:
                    pass
            return prompt("", multiline=False, highlighting="text")
        finally:
            PENDING_STDIN_PROMPT.set(False)
    else:
        return sys.stdin.readline()

class Stream(io.IOBase):
    """
    Custom io stream that intercepts stdout and stderr streams.

    This class manages text data by buffering it and optionally passing it through a hook
    for real-time processing and display. It ensures efficient data handling by
    maintaining a maximum buffer size and flushing data when this size is exceeded or on newlines.

    Args:
        shell: Shell instance whose hook attribute is consulted dynamically.
        hook_key: Key in ``shell.hooks`` (e.g. ``stdout_hook``).
        default_hook: Hook function used when the shell hook is unset.
        buffer_size (int, optional): Maximum size of the internal buffer before forcing a flush. Defaults to 2048.


    Attributes:
        shell: Shell instance whose hook attribute is consulted dynamically.
        buffer_size (int): Maximum size of the internal buffer before forcing a flush.
        buffer (str): Internal buffer for storing written data.
        cache_buffer (str): Buffer for storing all written data.

    Methods:
        write(data): Writes data to the stream, managing buffering and flushing.
        flush(data_to_flush=None): Flushes the given data to the hook and caches it.
        get_value(): Returns all text that has been written to this stream.
    """

    class _BinaryBufferProxy(io.BufferedIOBase):
        def __init__(self, parent: "Stream") -> None:
            super().__init__()
            self._parent = parent

        def writable(self) -> bool:
            return True

        def write(self, b) -> int:  # type: ignore[override]
            if b is None:
                return 0
            if isinstance(b, memoryview):
                b = b.tobytes()
            if isinstance(b, bytearray):
                b = bytes(b)
            if not isinstance(b, (bytes,)):
                raise TypeError(f"a bytes-like object is required, not '{type(b).__name__}'")
            text = self._parent._decode_bytes(b)
            return self._parent.write(text)

        def flush(self) -> None:  # type: ignore[override]
            self._parent.flush()

    def __init__(
        self,
        shell: object,
        *,
        hook_key: str,
        default_hook: Callable[[str, str, object], None],
        delegate: Optional[io.TextIOBase] = None,
        buffer_size: int = 2048,
    ) -> None:
        super().__init__()
        if shell is None:
            raise TypeError("Stream requires a shell instance")
        self._shell = shell
        self._hook_key = hook_key
        self._default_hook = default_hook
        self._delegate = delegate
        self._binary_buffer = Stream._BinaryBufferProxy(self)
        self.buffer_size = buffer_size
        self._pending_buffer = ""
        self.cache_buffer = ""

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        if self._delegate is not None and getattr(self._delegate, "encoding", None):
            return str(self._delegate.encoding)
        return "utf-8"

    @property
    def errors(self) -> str:
        if self._delegate is not None and getattr(self._delegate, "errors", None):
            return str(self._delegate.errors)
        return "replace"

    @property
    def newlines(self):
        if self._delegate is not None:
            return getattr(self._delegate, "newlines", None)
        return None

    @property
    def buffer(self) -> io.BufferedIOBase:
        return self._binary_buffer

    def isatty(self) -> bool:
        if self._delegate is not None and hasattr(self._delegate, "isatty"):
            try:
                return bool(self._delegate.isatty())
            except Exception:
                return False
        return False

    def fileno(self) -> int:
        if self._delegate is None or not hasattr(self._delegate, "fileno"):
            raise io.UnsupportedOperation("Stream does not expose a file descriptor")
        return int(self._delegate.fileno())

    def _decode_bytes(self, data: bytes) -> str:
        return data.decode(self.encoding, errors=self.errors)

    def write(self, data) -> int:  # type: ignore[override]
        """
        Writes data to the stream, managing buffering and flushing.

        Args:
            data (str): The data to be written to the stream.

        Raises:
            TypeError: If the input data is not a string.

        This method handles writing data to the stream, managing the internal buffer,
        and flushing complete lines or when the buffer size is exceeded.
        """
        if data is None:
            return 0
        if isinstance(data, (bytes, bytearray, memoryview)):
            if isinstance(data, memoryview):
                data = data.tobytes()
            if isinstance(data, bytearray):
                data = bytes(data)
            data = self._decode_bytes(data)
        if not isinstance(data, str):
            raise TypeError("write argument must be str, not {}".format(type(data).__name__))
        
        self._pending_buffer += data

        # Process complete lines
        lines = self._pending_buffer.split('\n')
        self._pending_buffer = lines.pop()  # Keep incomplete line in the buffer

        # Flush complete lines
        for line in lines:
            self.flush(line + '\n')

        # Handle buffer overflow
        while len(self._pending_buffer) > self.buffer_size:
            self.flush(self._pending_buffer[:self.buffer_size])
            self._pending_buffer = self._pending_buffer[self.buffer_size:]

        return len(data)

    def flush(self, data_to_flush: Optional[str] = None) -> None:
        """
        Flushes the given data to the hook and caches it.

        Args:
            data_to_flush (str, optional): The data to flush. If None, flushes the current buffer.

        This method processes the data through the hook (if set) and adds it to the cache buffer.
        """
        if data_to_flush is None:
            data_to_flush = self._pending_buffer
            self._pending_buffer = ""

        self.cache_buffer += data_to_flush

        if self._hook_key in ("stdout_hook", "stderr_hook") and SILENT_STDIO.get():
            return

        if self._hook_key == "stdout_hook":
            if data_to_flush and not data_to_flush.endswith("\n"):
                PENDING_STDIN_PROMPT.set(True)
            else:
                # Any newline ends the current terminal line; don't carry a pending prompt over it.
                PENDING_STDIN_PROMPT.set(False)

        hook = None
        hooks = getattr(self._shell, "hooks", None)
        if not isinstance(hooks, dict):
            raise TypeError("shell.hooks must be a dict")
        hook = hooks.get(self._hook_key)
        if hook is None:
            hook = self._default_hook
        if hook:
            ctx = RUN_CONTEXT.get()
            invoke = getattr(self._shell, "_invoke_hook", None)
            if callable(invoke):
                invoke(hook, data_to_flush, self.cache_buffer, ctx=ctx)
            else:
                hook(data_to_flush, self.cache_buffer, ctx)
        

    def get_value(self) -> str:
        """
        Returns all text that has been written to this stream.

        Returns:
            str: The entire content that has been written to the stream.
        """
        return self.cache_buffer


class StdinProxy(io.TextIOBase):
    """Proxy that feeds stdin through a provider callable.

    The hook is invoked whenever a new line is required and should return a
    string or ``None`` to signal EOF. Returned chunks are split into
    newline-terminated lines so consumers that read line-by-line keep working.
    """

    def __init__(
        self,
        shell: object,
        *,
        default_hook: Callable[[object], str],
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        if shell is None:
            raise TypeError("StdinProxy requires a shell instance")
        self._shell = shell
        self._default_hook = default_hook
        self._encoding = encoding
        self._buffer: deque[str] = deque()
        self._eof = False

    @property
    def encoding(self) -> str:
        return self._encoding

    def readable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise io.UnsupportedOperation("StdinProxy does not expose a file descriptor")

    def close(self):
        try:
            self._buffer.clear()
            self._eof = True
        finally:
            super().close()

    def _ensure_line(self):
        if self._buffer or self._eof:
            return

        hooks = getattr(self._shell, "hooks", None)
        if not isinstance(hooks, dict):
            raise TypeError("shell.hooks must be a dict")
        hook = hooks.get("stdin_hook")
        if hook is None:
            hook = self._default_hook
        if hook is not None and not callable(hook):
            raise TypeError("stdin_hook must be callable")

        ctx = RUN_CONTEXT.get()
        invoke = getattr(self._shell, "_invoke_hook", None)
        if callable(invoke):
            chunk = invoke(hook, ctx=ctx)
        else:
            chunk = hook(ctx)

        if chunk is None:
            self._eof = True
            return

        if not isinstance(chunk, str):
            raise TypeError("stdin provider must return str or None")

        if chunk == "":
            lines = ["\n"]
        else:
            lines = chunk.splitlines(True)
            if not lines:
                lines = ["\n"]
            if not lines[-1].endswith("\n"):
                lines[-1] += "\n"

        self._buffer.extend(lines)

    def readline(self, size=-1):
        self._checkClosed()

        if size == 0:
            return ""

        self._ensure_line()
        if not self._buffer:
            return ""

        line = self._buffer.popleft()
        if size > 0 and len(line) > size:
            remainder = line[size:]
            line = line[:size]
            self._buffer.appendleft(remainder)
        return line

    def read(self, size=-1):
        self._checkClosed()

        if size == 0:
            return ""

        if size < 0:
            return "".join(iter(self.readline, ""))

        parts = []
        remaining = size
        while remaining > 0:
            chunk = self.readline(remaining)
            if chunk == "":
                break
            parts.append(chunk)
            remaining -= len(chunk)
        return "".join(parts)


class StdIOProxy(io.TextIOBase):
    """A routing proxy for sys.stdout/sys.stderr.

    When a capture is active (via `CAPTURE_STREAMS`), writes are forwarded to the
    capture stream for this channel; otherwise they are forwarded to the delegate
    stream that was wrapped.
    """

    def __init__(self, delegate: io.TextIOBase, *, which: str) -> None:
        super().__init__()
        if which not in ("stdout", "stderr"):
            raise ValueError("which must be 'stdout' or 'stderr'")
        self._delegate = delegate
        self._index = 0 if which == "stdout" else 1

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._delegate, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._delegate, "errors", None) or "replace"

    @property
    def newlines(self):
        return getattr(self._delegate, "newlines", None)

    def isatty(self) -> bool:
        try:
            return bool(self._delegate.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return int(self._delegate.fileno())

    @property
    def buffer(self) -> io.BufferedIOBase:
        capture = CAPTURE_STREAMS.get()
        if capture is not None:
            sink = capture[self._index]
            buf = getattr(sink, "buffer", None)
            if buf is not None:
                return buf
        buf = getattr(self._delegate, "buffer", None)
        if buf is None:
            raise io.UnsupportedOperation("buffer is not available")
        return buf

    def flush(self) -> None:  # type: ignore[override]
        if getattr(self._delegate, "closed", False):
            return
        capture = CAPTURE_STREAMS.get()
        if capture is not None:
            sink = capture[self._index]
            if hasattr(sink, "flush"):
                try:
                    sink.flush()
                except ValueError:
                    return
                return
        try:
            self._delegate.flush()
        except ValueError:
            return

    def write(self, s: str) -> int:  # type: ignore[override]
        if s is None:
            return 0
        if not isinstance(s, str):
            raise TypeError(f"write argument must be str, not {type(s).__name__}")
        capture = CAPTURE_STREAMS.get()
        if capture is not None:
            sink = capture[self._index]
            return int(sink.write(s))
        return int(self._delegate.write(s))

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)


def install_stdio_proxy() -> None:
    """Install StdIOProxy on sys.stdout/sys.stderr if not already installed."""
    if not isinstance(sys.stdout, StdIOProxy):
        sys.stdout = StdIOProxy(sys.stdout, which="stdout")
    if not isinstance(sys.stderr, StdIOProxy):
        sys.stderr = StdIOProxy(sys.stderr, which="stderr")
