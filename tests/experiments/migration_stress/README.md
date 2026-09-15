# Migration-mode repros

Standalone repros for `docs/dev/migration_mode_failures.md`.  Each needs a
CPython built with BOTH `src/patches` and the extension built against it with
`CFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME"`; run from the repo root:

```sh
PYTHONPATH=src PYTHON_GIL=0 RUNLOOM_MIGRATION=1 <patched-python> tests/experiments/migration_stress/<script>
```

| script | expected with `RUNLOOM_MIGRATION=1` | with it unset |
| --- | --- | --- |
| `heavy_exit_crash.py` | prints `OK` then dies with SIGSEGV in `Py_FinalizeEx` (exit 139; deterministic with the sysmon env the script sets, ~1 in 3 without) | exits 0 |
| `deadlock_detector_off.py` | never raises; kill it | `DEADLOCK RAISED after ~0.25 s` |
| `bulk_arena_hang.py` | hangs in `WaitGroup.wait` (faulthandler dumps at 8 s and exits) | `bulk woke ... seen 32 OK` |

The pytest deadlock files pass with `RUNLOOM_SYSMON=1`, but this standalone probe
still hangs in most runs with it (see the doc); the other two scripts are
unaffected by sysmon.
