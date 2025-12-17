import hashlib
import random
import string
import sys
from typing import Any
from threading import Thread as ThreadBase


def content_hash(content: str) -> str:
    """Return a SHA256 hex digest for the provided content string."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()

def short_id(length: int = 8) -> str:
    """Generate a short pseudo-random identifier."""
    return "".join(random.choices(string.ascii_letters, k=length))

def debug_print(*args: Any, sep: str = " ", end: str = "\n", flush: bool = False) -> None:
    """Write directly to the real stdout, bypassing patched streams."""
    sys.__stdout__.write(sep.join(map(str, args)) + end)
    if flush:
        sys.__stdout__.flush()

def is_running_in_streamlit_runtime():
    try:
        import streamlit.runtime.scriptrunner
        return True
    except Exception:
        return False

def Thread(*args,**kwargs):
    thread=ThreadBase(*args,**kwargs)
    if is_running_in_streamlit_runtime():
        from streamlit.runtime.scriptrunner import get_script_run_ctx,add_script_run_ctx
        ctx=get_script_run_ctx()
        add_script_run_ctx(thread,ctx)
    return thread
    
