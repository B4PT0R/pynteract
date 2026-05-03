import sys
import os

# Ensure the local dev copy is imported, not the installed package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pynteract import Shell


@pytest.fixture
def shell():
    """Fresh Shell instance for each test."""
    return Shell()


@pytest.fixture
def stateful_shell():
    """Shell with pre-seeded namespace (x=42) for state-persistence tests."""
    s = Shell()
    s.run("x = 42")
    return s
