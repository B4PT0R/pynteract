from __future__ import annotations

import builtins as _builtins
import sys
import uuid
import threading
from weakref import WeakKeyDictionary
from typing import Any


class DummyModule:
    """A lightweight module-like wrapper whose ``__dict__`` is user-provided.

    This mirrors IPython's approach: when embedders provide a dict for the user
    namespace, we keep that exact dict as the module namespace (reference
    preserved) while still registering a module-like object in ``sys.modules``.
    """

    def __init__(self, name: str, namespace: dict[str, Any]) -> None:
        self.__dict__ = namespace
        self.__name__ = name


def _ensure_cwd_on_syspath() -> None:
    """Mimic interactive Python import behavior by ensuring '' is on sys.path."""
    if "" not in sys.path:
        sys.path.insert(0, "")


def _ensure_identity_keys(namespace: dict[str, Any], *, module_name: str, filename: str) -> None:
    namespace.setdefault("__builtins__", _builtins)
    namespace.setdefault("__doc__", None)
    namespace.setdefault("__package__", None)
    namespace.setdefault("__spec__", None)
    namespace["__name__"] = module_name
    namespace["__file__"] = filename


def _ensure_module(name: str, *, namespace: dict[str, Any], filename: str) -> Any:
    """Get or create a module-like object in sys.modules bound to ``namespace``."""
    mod = sys.modules.get(name)
    if mod is None or not hasattr(mod, "__dict__") or getattr(mod, "__dict__") is not namespace:
        mod = DummyModule(name, namespace)
        sys.modules[name] = mod
    _ensure_identity_keys(namespace, module_name=name, filename=filename)
    return mod


def _choose_module_name(
    namespace: dict[str, Any],
    *,
    preferred: str | None,
    auto_module_prefix: str = "__pynteract__",
) -> str:
    if isinstance(preferred, str) and preferred:
        return preferred

    existing = namespace.get("__name__")
    if isinstance(existing, str) and existing:
        return existing

    candidate = auto_module_prefix
    if candidate not in sys.modules:
        return candidate

    # Avoid collisions with an existing module; keep trying until we find a free slot.
    while True:
        candidate = f"{auto_module_prefix}_{uuid.uuid4().hex[:12]}__"
        if candidate not in sys.modules:
            return candidate


_MISSING = object()
_released_modules = WeakKeyDictionary()


class NamespaceManager:
    """Manage a single dict execution namespace registered in ``sys.modules``.

    The shell executes code against ``namespace`` (a dict). We also register a
    module-like object in ``sys.modules`` whose ``__dict__`` is that same dict
    (reference preserved), enabling module-level expectations like ``__name__``
    and ``__file__`` plus import-related behaviours.

    Registrations installed by this manager are released explicitly by ``close``.
    Existing modules backed by the same dict remain caller-owned. Structural
    namespace mutations are serialized; Shell also guards execution ownership.
    """

    def __init__(
        self,
        *,
        module_name: str | None = None,
        filename: str = "<shell-input-0>",
        ensure_cwd_on_syspath: bool = True,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        if ensure_cwd_on_syspath:
            _ensure_cwd_on_syspath()

        self._lock = threading.Lock()
        self._registrations = {}
        self.namespace: dict[str, Any] = {} if namespace is None else namespace
        self.module_name = _choose_module_name(self.namespace, preferred=module_name)
        self.current_filename = filename
        self.module = self.bind_namespace(self.namespace, filename=filename, module_name=self.module_name)

    def bind_namespace(self, namespace, *, filename, module_name=None):
        """Register a namespace, retaining only registrations we actually install."""
        name = _choose_module_name(namespace, preferred=module_name)
        previous = sys.modules.get(name, _MISSING)
        module = _ensure_module(name, namespace=namespace, filename=filename)
        if module is not previous:
            original, owned = self._registrations.get(name, (previous, None))
            if owned is not None:
                _released_modules[owned] = original
            if previous is not owned:
                original = previous
            self._registrations[name] = (original, module)
        return module

    def close(self):
        """Restore only slots that still contain our exact module object."""
        with self._lock:
            for name, (original, owned) in self._registrations.items():
                # A later shell may have temporarily replaced our registration.
                # Its eventual close must not resurrect this already closed one.
                _released_modules[owned] = original
                while isinstance(original, DummyModule) and original in _released_modules:
                    original = _released_modules[original]
                if sys.modules.get(name, _MISSING) is owned:
                    if original is _MISSING:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = original
            self._registrations.clear()

    @staticmethod
    def prepare_namespace(
        namespace: dict[str, Any],
        *,
        filename: str,
        module_name: str | None = None,
        auto_module_prefix: str = "__pynteract_run_",
    ) -> str:
        """Ensure `namespace` behaves like a module __dict__ and is registered in sys.modules.

        If `module_name` is None, we reuse `namespace["__name__"]` when present, otherwise we
        generate a unique synthetic name and store it back into `namespace["__name__"]`.
        """
        chosen = module_name
        if not isinstance(chosen, str) or not chosen:
            chosen = namespace.get("__name__") if isinstance(namespace.get("__name__"), str) else ""

        if not chosen:
            while True:
                candidate = f"{auto_module_prefix}{uuid.uuid4().hex[:12]}__"
                mod = sys.modules.get(candidate)
                if mod is None or getattr(mod, "__dict__", None) is namespace:
                    chosen = candidate
                    break
            namespace["__name__"] = chosen

        _ensure_module(chosen, namespace=namespace, filename=filename)
        return chosen

    def set_current_filename(self, filename: str) -> None:
        self.current_filename = filename
        self.namespace["__file__"] = filename

    def reset_module_namespace(self) -> None:
        """Clear namespace atomically (preserve module identity keys).

        Protected by ``self._lock`` to prevent concurrent ``arun()`` calls
        from observing the namespace mid-clear.
        """
        with self._lock:
            self.namespace.clear()
            _ensure_identity_keys(
                self.namespace,
                module_name=self.module_name,
                filename=self.current_filename,
            )
            self.module = self.bind_namespace(
                self.namespace,
                module_name=self.module_name,
                filename=self.current_filename,
            )

    def set_namespace(self, namespace: dict[str, Any]) -> None:
        """Replace the namespace dict atomically (reference preserved).

        Protected by ``self._lock`` to prevent a concurrent ``arun()`` from
        executing against the old namespace after the swap.
        """
        with self._lock:
            self.namespace = namespace
            self.module = self.bind_namespace(
                self.namespace,
                module_name=self.module_name,
                filename=self.current_filename,
            )
