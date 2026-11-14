"""Subprocess streaming with owned readers and error propagation."""
import asyncio
import codecs
import locale
import os
import queue
import signal
import subprocess
import sys

from .utils import Thread


def _kill(process):
    # Descendants can inherit the pipes. On POSIX own a process group so an
    # aborted reader cannot leave us joining a pipe held by a surviving child.
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass


def _kill_async_process(process):
    """Kill an asyncio subprocess and its POSIX process group if still alive."""
    try:
        if os.name == "posix":
            # The group may outlive its leader while a descendant still holds a
            # captured pipe open, so do not gate this on process.returncode.
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass


def run_streamed(command, *, shell=False, stdout=None, prepare_thread=None):
    """Stream both pipes; propagate reader/hook errors after reaping the child.

    This is deliberately synchronous, including when called in an async cell.
    POSIX failures kill the owned process group; Windows kills the direct child.
    """
    process = subprocess.Popen(
        command, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, start_new_session=(os.name == "posix"),
    )
    errors = queue.SimpleQueue()
    started = []

    def read(stream, target):
        try:
            for line in iter(stream.readline, ""):
                target.write(line)
                target.flush()
        except BaseException as exc:
            errors.put(exc)
        finally:
            stream.close()

    try:
        readers = [Thread(target=read, args=(process.stdout, sys.stdout if stdout is None else stdout)),
                   Thread(target=read, args=(process.stderr, sys.stderr))]
        for thread in readers:
            if prepare_thread is not None:
                prepare_thread(thread)
        for thread in readers:
            thread.start()
            started.append(thread)
        while process.poll() is None or any(thread.is_alive() for thread in started):
            if not errors.empty():
                raise errors.get()
            for thread in started:
                thread.join(timeout=0.02)
        if not errors.empty():
            raise errors.get()
        return process.wait()
    except BaseException:
        _kill(process)
        raise
    finally:
        process.wait()
        for thread in started:
            thread.join()
        process.stdout.close()
        process.stderr.close()


async def run_streamed_async(command, *, shell=False, stdout=None):
    """Run a subprocess without blocking the event loop, streaming both pipes."""
    options = dict(
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    if shell:
        process = await asyncio.create_subprocess_shell(command, **options)
    else:
        args = [command] if isinstance(command, (str, bytes, os.PathLike)) else command
        process = await asyncio.create_subprocess_exec(*args, **options)

    encoding = locale.getpreferredencoding(False)

    async def read(stream, target):
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        while data := await stream.read(8192):
            text = decoder.decode(data)
            if text:
                target.write(text)
                target.flush()
        final = decoder.decode(b"", final=True)
        if final:
            target.write(final)
            target.flush()

    readers = [
        asyncio.create_task(read(process.stdout, sys.stdout if stdout is None else stdout)),
        asyncio.create_task(read(process.stderr, sys.stderr)),
    ]
    waiter = asyncio.create_task(process.wait())
    try:
        await asyncio.gather(waiter, *readers)
        return waiter.result()
    except BaseException:
        _kill_async_process(process)
        await process.wait()
        await asyncio.gather(waiter, return_exceptions=True)
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        raise
