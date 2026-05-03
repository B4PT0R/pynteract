"""Tests de non-régression pour namespace_utils.py."""
import sys
import threading
import pytest
from pynteract.namespace_utils import (
    NamespaceManager,
    DummyModule,
    _ensure_cwd_on_syspath,
    _ensure_identity_keys,
    _ensure_module,
    _choose_module_name,
)


# ── DummyModule ────────────────────────────────────────────────────────────────

class TestDummyModule:
    def test_dict_is_namespace(self):
        ns = {"x": 1}
        mod = DummyModule("test_mod", ns)
        assert mod.__dict__ is ns

    def test_name(self):
        mod = DummyModule("mymod", {})
        assert mod.__name__ == "mymod"

    def test_mutations_reflected(self):
        ns = {}
        mod = DummyModule("m", ns)
        ns["y"] = 42
        assert mod.y == 42


# ── _ensure_cwd_on_syspath ────────────────────────────────────────────────────

class TestEnsureCwdOnSyspath:
    def test_adds_empty_string(self):
        original = sys.path.copy()
        try:
            if "" in sys.path:
                sys.path.remove("")
            _ensure_cwd_on_syspath()
            assert "" in sys.path
        finally:
            sys.path[:] = original

    def test_idempotent(self):
        _ensure_cwd_on_syspath()
        _ensure_cwd_on_syspath()
        assert sys.path.count("") == 1


# ── _ensure_identity_keys ─────────────────────────────────────────────────────

class TestEnsureIdentityKeys:
    def test_sets_name_and_file(self):
        ns = {}
        _ensure_identity_keys(ns, module_name="mymod", filename="myfile.py")
        assert ns["__name__"] == "mymod"
        assert ns["__file__"] == "myfile.py"

    def test_sets_builtins_default(self):
        ns = {}
        _ensure_identity_keys(ns, module_name="m", filename="f")
        assert "__builtins__" in ns

    def test_does_not_overwrite_existing_builtins(self):
        sentinel = object()
        ns = {"__builtins__": sentinel}
        _ensure_identity_keys(ns, module_name="m", filename="f")
        assert ns["__builtins__"] is sentinel

    def test_overwrites_name(self):
        ns = {"__name__": "old"}
        _ensure_identity_keys(ns, module_name="new", filename="f")
        assert ns["__name__"] == "new"


# ── _ensure_module ────────────────────────────────────────────────────────────

class TestEnsureModule:
    def test_registers_in_sys_modules(self):
        name = "__pynteract_test_ensure__"
        ns = {}
        try:
            mod = _ensure_module(name, namespace=ns, filename="<test>")
            assert name in sys.modules
            assert sys.modules[name] is mod
        finally:
            sys.modules.pop(name, None)

    def test_reuses_existing_module(self):
        name = "__pynteract_test_reuse__"
        ns = {}
        try:
            mod1 = _ensure_module(name, namespace=ns, filename="<test>")
            mod2 = _ensure_module(name, namespace=ns, filename="<test>")
            assert mod1 is mod2
        finally:
            sys.modules.pop(name, None)

    def test_replaces_module_with_different_namespace(self):
        name = "__pynteract_test_replace__"
        ns1, ns2 = {}, {}
        try:
            _ensure_module(name, namespace=ns1, filename="<test>")
            mod2 = _ensure_module(name, namespace=ns2, filename="<test>")
            assert sys.modules[name] is mod2
            assert mod2.__dict__ is ns2
        finally:
            sys.modules.pop(name, None)


# ── _choose_module_name ───────────────────────────────────────────────────────

class TestChooseModuleName:
    def test_preferred_wins(self):
        name = _choose_module_name({}, preferred="myname")
        assert name == "myname"

    def test_existing_name_in_namespace(self):
        name = _choose_module_name({"__name__": "existing"}, preferred=None)
        assert name == "existing"

    def test_generates_unique_name(self):
        name = _choose_module_name({}, preferred=None)
        assert name.startswith("__pynteract__") or "__pynteract__" in name

    def test_no_collision(self):
        sentinel = "__pynteract__"
        try:
            sys.modules[sentinel] = object()  # type: ignore
            name = _choose_module_name({}, preferred=None)
            assert name != sentinel
        finally:
            sys.modules.pop(sentinel, None)


# ── NamespaceManager ──────────────────────────────────────────────────────────

class TestNamespaceManager:
    def _make(self, **kwargs):
        name = f"__pynteract_nm_test_{id(self)}__"
        mgr = NamespaceManager(module_name=name, **kwargs)
        return mgr, name

    def teardown_method(self, _):
        for key in list(sys.modules):
            if key.startswith("__pynteract_nm_test_"):
                sys.modules.pop(key, None)

    def test_namespace_registered(self):
        mgr, name = self._make()
        assert name in sys.modules
        assert sys.modules[name].__dict__ is mgr.namespace

    def test_custom_namespace(self):
        ns = {"x": 42}
        mgr, _ = self._make(namespace=ns)
        assert mgr.namespace is ns
        assert mgr.namespace["x"] == 42

    def test_reset_clears_namespace(self):
        mgr, _ = self._make()
        mgr.namespace["user_var"] = "hello"
        mgr.reset_module_namespace()
        assert "user_var" not in mgr.namespace

    def test_reset_preserves_identity_keys(self):
        mgr, name = self._make()
        mgr.reset_module_namespace()
        assert mgr.namespace["__name__"] == name
        assert "__builtins__" in mgr.namespace

    def test_set_namespace_swaps(self):
        mgr, _ = self._make()
        new_ns = {"z": 99}
        mgr.set_namespace(new_ns)
        assert mgr.namespace is new_ns

    def test_set_current_filename(self):
        mgr, _ = self._make()
        mgr.set_current_filename("new_file.py")
        assert mgr.current_filename == "new_file.py"
        assert mgr.namespace["__file__"] == "new_file.py"

    # -- thread safety --

    def test_reset_thread_safe(self):
        """Concurrent resets must not corrupt the namespace."""
        mgr, _ = self._make()
        errors = []

        def resetter():
            try:
                for _ in range(50):
                    mgr.namespace["tmp"] = "before"
                    mgr.reset_module_namespace()
                    # After reset, identity keys must always be present
                    assert "__name__" in mgr.namespace
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=resetter) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors, f"Thread safety errors: {errors}"

    def test_set_namespace_thread_safe(self):
        """Concurrent set_namespace calls must not lose the lock."""
        mgr, _ = self._make()
        errors = []

        def swapper():
            try:
                for _ in range(50):
                    mgr.set_namespace({"x": 1})
                    assert mgr.namespace is not None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=swapper) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors

    def test_prepare_namespace_registers_module(self):
        name = "__pynteract_prepare_test__"
        ns = {}
        try:
            chosen = NamespaceManager.prepare_namespace(ns, filename="<test>", module_name=name)
            assert chosen == name
            assert name in sys.modules
        finally:
            sys.modules.pop(name, None)

    def test_prepare_namespace_reuses_existing_name(self):
        ns = {"__name__": "__existing_name__"}
        try:
            chosen = NamespaceManager.prepare_namespace(ns, filename="<test>")
            assert chosen == "__existing_name__"
        finally:
            sys.modules.pop("__existing_name__", None)

    def test_prepare_namespace_generates_unique(self):
        ns = {}
        try:
            chosen = NamespaceManager.prepare_namespace(ns, filename="<test>")
            assert chosen in sys.modules
        finally:
            sys.modules.pop(chosen, None)
