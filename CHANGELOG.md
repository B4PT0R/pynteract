# Changelog

## 1.1.0 - 2026-10-26

- Add `Shell.interrupt()` and export `ExecutionInterrupted` for cooperative host
  interruption without closing the shell or replacing its namespace. Support
  async interruption from another task/thread and synchronous interruption from
  the owning thread, without installing process-wide signal handlers.
- Preserve completed namespace mutations and run cleanup on interruption. Keep
  external async cancellation distinct and remove only cancellation issued by
  the shell, including when user code suppresses it.
- Document interruption limits and add regression coverage for signal delivery,
  cross-thread calls, async cleanup, and shell reuse after interruption.

## 1.0.0 - 2026-10-25

- Register terminal key bindings before first input, implement clipboard copy/cut, and stop retrying terminal initialization failures. Include Pygments in the terminal extra. Respect startup encoding declarations, handle optional config/read errors once, and avoid repeating announced failures. Add startup/terminal regressions, Linux multi-Python CI and built-wheel smoke checks.

- Preserve Python quotes/spacing in `%timeit`, validate leading iteration options, accept tab-separated line magics, and keep cell-magic bodies opaque to other syntax transforms. Parse nested/quoted template braces; clarify that curated eval builtins are not a sandbox.

- Add explicit `Shell.close()` and context-manager lifetime, with ownership-aware module restoration and automatic CLI cleanup. Preserve inspectable cell sources until close, independently of history eviction; isolate colliding filenames without overwriting host files or caches.

- Include nested run output in parent responses without duplicate hook delivery; propagate `%run` failures and display their diagnostic once. Respect Python script encodings and sibling imports; restore script process state on all exit paths, and map CLI `SystemExit` to an exit code.

- Start enriched tracebacks at the first compiled cell frame, preserving downstream library calls, chains and exception-group details without mutating the original traceback. Compilation errors show source diagnostics instead of parser/compiler internals; failures before cell execution retain their diagnostic stack.

- Export `get_run_ctx()` and `RunContext`; hooks now receive payload arguments only (breaking change: remove the final routing context argument).
- Restore standard streams even if an output hook fails during final flushing.
- Let cancellation and interpreter control flow propagate instead of capturing them as ordinary cell errors.
- Reset execution context even if working-directory restoration fails.
- Execute `arun()` natively in the caller's event loop/task, supporting top-level await/async for/async with and retaining original Python results. Blocking code must be explicitly offloaded by the embedder (breaking change from the implicit threadpool prototype).
- Share preparation, AST compilation and response finalization between sync and async execution.
- Route stdout/stderr/stdin by execution context with scoped shared proxies; propagate context to command-reader threads.
- Own, order and drain async stdout/stderr hook delivery before returning; propagate delivery failures and cancel/await the worker on cancellation. Reject async hooks in synchronous-only positions instead of dropping output.
- Default to shared process cwd (`use_internal_cwd=False`) without implicit `chdir`; internal-cwd restoration is now opt-in (breaking change).
- Reject overlapping use of one Shell and unsafe process-wide cwd isolation; support concurrent separate shells in ambient-cwd mode, and preserve inline nested `%run` execution state.
- Keep `ShellResponse.input` equal to the original source even when magics/system commands are expanded.
- Document process-state limits, cooperative cancellation, explicit thread offloading and the absence of sandbox isolation.

## 0.1.2 - 2026-05-03

- Add `Shell(silent=True)` as an instance-level default for suppressing stdout/stderr hooks.
- Keep per-call `run(..., silent=...)` and `arun(..., silent=...)` overrides, with `None` inheriting the instance default.
- Document the constructor-level `silent` option.
