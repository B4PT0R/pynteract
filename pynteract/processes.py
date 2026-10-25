"""Blocking subprocess streaming with owned readers and error propagation."""
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
