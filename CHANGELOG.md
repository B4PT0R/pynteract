# Changelog

## 0.1.2 - 2026-05-03

- Add `Shell(silent=True)` as an instance-level default for suppressing stdout/stderr hooks.
- Keep per-call `run(..., silent=...)` and `arun(..., silent=...)` overrides, with `None` inheriting the instance default.
- Document the constructor-level `silent` option.
