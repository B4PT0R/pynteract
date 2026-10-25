"""Explicitly owned linecache entries for live interactive code."""
import linecache
import os
import uuid


class SourceCache:
    def __init__(self):
        self.entries = {}
        self.names = {}

    def register(self, filename, text):
        # Never replace host cache entries or shadow an actual source file.
        # Reused cell names need independent sources for still-live functions.
        internal = filename
        if internal in linecache.cache or os.path.exists(internal):
            internal = f"<pynteract-{uuid.uuid4().hex}:{filename}>"
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        entry = (len(text), None, lines, internal)
        linecache.cache[internal] = entry
        self.entries[internal] = entry
        self.names[internal] = filename
        return internal

    def close(self):
        for filename, entry in self.entries.items():
            if linecache.cache.get(filename) is entry:
                linecache.cache.pop(filename, None)
        self.entries.clear()
        self.names.clear()
