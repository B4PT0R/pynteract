import io
import asyncio
import contextvars
import inspect
import threading
from contextlib import contextmanager
from typing import Optional, Callable
from collections import deque
import sys
from .ptk import prompt
from .utils import call_sync_hook
from .context import CAPTURE_STDIN, CAPTURE_STREAMS, SILENT_STDIO, PENDING_STDIN_PROMPT

def stdout_write(data: str, buffer: str) -> None:
    """Default stdout hook that mirrors captured output to the real stdout."""
    if isinstance(sys.stdout, (Stream, StdIOProxy)):
        sys.__stdout__.write(data)
        sys.__stdout__.flush()
    else:
        sys.stdout.write(data)
        sys.stdout.flush()

def stderr_write(data: str, buffer: str) -> None:
    """Default stderr hook that mirrors captured output to the real stderr."""
    if isinstance(sys.stderr, (Stream, StdIOProxy)):
        sys.__stderr__.write(data)
        sys.__stderr__.flush()
    else:
        sys.stderr.write(data)
        sys.stderr.flush()

def stdin_readline() -> str:
    """Fallback stdin hook that reads from the console."""
    if isinstance(sys.stdin, (StdinProxy, StdinRoutingProxy)):
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


class AsyncHookQueue:
    """One ordered, owned stream-hook worker per async Collector."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.pending = deque()
        self.ready = asyncio.Event()
        self.task = None
        self.error = None
        self.closed = False
        self.lock = threading.Lock()

    def submit(self, hook, data, stream, end):
        with self.lock:
            if self.closed:
                raise RuntimeError("Async stream capture has already finished")
            # Store an offset, not a cumulative string per chunk. A slow hook
            # must not retain quadratically many copies of captured output.
            self.pending.append((hook, data, stream, end))
        self.loop.call_soon_threadsafe(self._start)

    def _start(self):
        if self.task is None and self.pending:
            ctx = contextvars.copy_context()
            ctx.run(CAPTURE_STREAMS.set, None)
            ctx.run(CAPTURE_STDIN.set, None)
            self.task = self.loop.create_task(self._work(), context=ctx)
        self.ready.set()

    async def _work(self):
        while True:
            with self.lock:
                if self.pending:
                    item = self.pending.popleft()
                elif self.closed:
                    return
                else:
                    item = None
                    self.ready.clear()
            if item is None:
                await self.ready.wait()
                continue
            hook, data, stream, end = item
            try:
                await hook(data, stream._snapshot(end))
            except Exception as exc:
                if self.error is None:
                    self.error = exc

    async def drain(self):
        with self.lock:
            self.closed = True
        self._start()
        try:
            if self.task is not None:
                await self.task
        except BaseException:
            await self.cancel()
            raise
        if self.error is not None:
            raise self.error

    async def cancel(self):
        with self.lock:
            self.closed = True
            self.pending.clear()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class Stream(io.IOBase):
    """
    Custom io stream that intercepts stdout and stderr streams.

    This class manages text data by buffering it and optionally passing it through a hook
    for real-time processing and display. It ensures efficient data handling by
    maintaining a maximum buffer size and flushing data when this size is exceeded or on newlines.

    Async stdout/stderr hooks are queued by an async Collector and drained in
    emission order before it exits. Sync capture requires synchronous hooks.

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
        default_hook: Callable[[str, str], None],
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
        self._cache = io.StringIO()
        self._cache_lock = threading.Lock()
        self.async_hooks = None
        self._capture_parent = None

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

        # Flush if buffer exceeds size limit
        if len(self._pending_buffer) >= self.buffer_size:
            self.flush()

        return len(data)

    def flush(self, data_to_flush=None) -> None:  # type: ignore[override]
        """Flush buffered data through the hook (sync or async).

        Synchronous hooks run immediately. Async hooks are submitted to the
        owning async Collector's ordered queue; its exit waits for completion.
        Without an async Collector they raise TypeError rather than dropping IO.

        Args:
            data_to_flush (str, optional): The data to flush. If None, flushes the current buffer.
        """
        if data_to_flush is None:
            data_to_flush = self._pending_buffer
            self._pending_buffer = ""

        if not data_to_flush:
            return

        end = self._record(data_to_flush)

        if self._hook_key in ("stdout_hook", "stderr_hook") and SILENT_STDIO.get():
            return

        if self._hook_key == "stdout_hook":
            if data_to_flush and not data_to_flush.endswith("\n"):
                PENDING_STDIN_PROMPT.set(True)
            else:
                # Any newline ends the current terminal line; don't carry a pending prompt over it.
                PENDING_STDIN_PROMPT.set(False)

        hooks = getattr(self._shell, "hooks", None)
        if not isinstance(hooks, dict):
            raise TypeError("shell.hooks must be a dict")
        hook = hooks.get(self._hook_key)
        if hook is None:
            hook = self._default_hook
        if hook is None:
            return

        if inspect.iscoroutinefunction(hook) or inspect.iscoroutinefunction(getattr(hook, "__call__", None)):
            if self.async_hooks is None:
                raise TypeError("Async stream hooks require Shell.arun() or async with Collector")
            self.async_hooks.submit(hook, data_to_flush, self, end)
        else:
            # Hook output belongs to the host, not recursively to this capture.
            token = CAPTURE_STREAMS.set(None)
            try:
                if hook in (stdout_write, stderr_write) and self._delegate is not None:
                    delegate = self._delegate
                    while isinstance(delegate, StdIOProxy):
                        delegate = delegate._delegate
                    delegate.write(data_to_flush)
                    delegate.flush()
                else:
                    call_sync_hook(hook, data_to_flush, self._snapshot(end))
            finally:
                CAPTURE_STREAMS.reset(token)

    def _record(self, data):
        """Capture in each enclosing run, but deliver hooks only once."""
        with self._cache_lock:
            self._cache.seek(0, io.SEEK_END)
            self._cache.write(data)
            end = self._cache.tell()
        if self._capture_parent is not None:
            self._capture_parent._record(data)
        return end

    def _snapshot(self, end=None) -> str:
        with self._cache_lock:
            self._cache.seek(0)
            return self._cache.read(end)

    @property
    def cache_buffer(self) -> str:
        """Captured output so far (materialized only on demand)."""
        return self._snapshot()

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
        default_hook: Callable[[], str],
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

        chunk = call_sync_hook(hook)

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


class StdinRoutingProxy:
    """Route synchronous reads to the current task's stdin provider."""

    def __init__(self, delegate):
        self._delegate = delegate

    def __getattr__(self, name):
        return getattr(CAPTURE_STDIN.get() or self._delegate, name)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line


_capture_lock = threading.Lock()
_capture_depth = 0
_capture_saved = None


@contextmanager
def capture_stdio(stdout, stderr, stdin):
    """Share scoped routing proxies across overlapping captures, restoring exactly."""
    global _capture_depth, _capture_saved
    with _capture_lock:
        if not _capture_depth:
            _capture_saved = sys.stdout, sys.stderr, sys.stdin
            install_stdio_proxy()
            sys.stdin = StdinRoutingProxy(sys.stdin)
        _capture_depth += 1
    output_token = CAPTURE_STREAMS.set((stdout, stderr))
    input_token = CAPTURE_STDIN.set(stdin)
    try:
        yield
    finally:
        CAPTURE_STDIN.reset(input_token)
        CAPTURE_STREAMS.reset(output_token)
        with _capture_lock:
            _capture_depth -= 1
            if not _capture_depth:
                sys.stdout, sys.stderr, sys.stdin = _capture_saved
                _capture_saved = None
