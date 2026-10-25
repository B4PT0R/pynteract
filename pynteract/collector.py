import sys
from .streams import (Stream, StdinProxy, stdout_write, stderr_write, stdin_readline,
                      capture_stdio, AsyncHookQueue)
from .utils import call_sync_hook
from .context import CAPTURE_STREAMS

class Collector:
    """
    Manages stdout and stderr redirection within a context manager.

    Captures Python-level IO and ordinary exceptions, with task-local routing.
    ``async with`` also owns and drains an ordered async stdout/stderr hook queue.
    Control-flow exceptions and cancellation propagate; stream restoration is
    guaranteed even when final flushing or hook delivery fails.

    Attributes:
        shell: Shell instance whose hooks are consulted dynamically.
        stdout_stream (Stream): Custom stream for capturing stdout.
        stderr_stream (Stream): Custom stream for capturing stderr.
        exception (Exception): Stores any exception raised during execution.

    Methods:
        get_stdout(): Returns all that was written to the stdout stream.
        get_stderr(): Returns all that was written to the stderr stream.
    """
    def __init__(self, shell):
        if shell is None:
            raise TypeError("Collector requires a shell instance")
        self.shell = shell
        delegate_stdout = sys.stdout
        delegate_stderr = sys.stderr
        self.stdin_proxy = StdinProxy(shell, default_hook=stdin_readline)
        self.stdout_stream = Stream(shell, hook_key="stdout_hook", default_hook=stdout_write, delegate=delegate_stdout)
        self.stderr_stream = Stream(shell, hook_key="stderr_hook", default_hook=stderr_write, delegate=delegate_stderr)
        self.exception = None
        self._reported = {}
        self.stdout_stream._collector = self

    def get_stdout(self):
        """
        Returns all that was written to the stdout stream.

        Returns:
            str: The entire content written to stdout.
        """
        return self.stdout_stream.get_value()
    
    def get_stderr(self):
        """
        Returns all that was written to the stderr stream.

        Returns:
            str: The entire content written to stderr.
        """
        return self.stderr_stream.get_value()

    # ------------------------------------------------------------------ sync --

    def __enter__(self):
        """
        Implements using the collector as a context manager.

        Scoped shared proxies route stdout, stderr and stdin through the current
        ContextVars. Other tasks and threads keep using the hosting streams.

        Returns:
            Collector: The Collector instance.
        """
        parent_streams = CAPTURE_STREAMS.get()
        if parent_streams is not None:
            for child, parent in zip((self.stdout_stream, self.stderr_stream), parent_streams):
                parent.flush()
                child._capture_parent = parent
                if child.async_hooks is None:
                    child.async_hooks = parent.async_hooks
            parent = getattr(parent_streams[0], '_collector', None)
            if parent is not None:
                self._reported = parent._reported
        self._capture = capture_stdio(self.stdout_stream, self.stderr_stream, self.stdin_proxy)
        self._capture.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        """
        Flushes the streams, restores the standard streams and gracefully processes any pending exception.

        Args:
            exc_type: The type of the exception.
            exc_value: The exception instance.
            exc_traceback: The traceback object.

        Returns:
            bool: True if the exception was handled, False otherwise.
        """
        # A failing output hook must never leave the hosting process redirected.
        try:
            try:
                self.stdout_stream.flush()
            finally:
                self.stderr_stream.flush()
        finally:
            self._capture.__exit__(None, None, None)

        # Cancellation and interpreter control flow are not cell errors.
        if exc_type is not None and issubclass(exc_type, Exception):
            # Save the exception
            self.exception = exc_value
            if self._reported.get(id(exc_value)) is not exc_value:
                message, enriched = self.shell._build_enriched_traceback(exc_type, exc_value, exc_traceback)
                exc_value.enriched_traceback = enriched
                exc_value.enriched_traceback_string = message
                # A propagated nested failure is displayed once, at its cell root.
                self._reported[id(exc_value)] = exc_value
                self.stderr_stream.write(message)
                self.stderr_stream.flush()
            # Send the exception to the exception hook
            hooks = getattr(self.shell, "hooks", None)
            if not isinstance(hooks, dict):
                raise TypeError("shell.hooks must be a dict")
            exception_hook = hooks.get("exception_hook")
            if exception_hook:
                call_sync_hook(exception_hook, exc_value)
            return True  # Suppress exception propagation

    # ----------------------------------------------------------------- async --

    async def __aenter__(self):
        self.async_hooks = AsyncHookQueue()
        self.stdout_stream.async_hooks = self.async_hooks
        self.stderr_stream.async_hooks = self.async_hooks
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_value, exc_traceback):
        try:
            handled = self.__exit__(exc_type, exc_value, exc_traceback)
        except BaseException:
            await self.async_hooks.cancel()
            raise
        if exc_type is not None and not issubclass(exc_type, Exception):
            await self.async_hooks.cancel()
        else:
            await self.async_hooks.drain()
        return handled
