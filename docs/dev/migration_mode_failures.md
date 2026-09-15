# Migration mode (`RUNLOOM_MIGRATION=1`): what fails, and why

Stress pass of 2026-09-15 against a fully patched, production-optimized
free-threaded interpreter (`3.14.4t-mig`: alloc-home + exec-home,
`--enable-optimizations --with-lto=thin --with-tail-call-interp`), runloom at
main `8a5f9de8`.  The runloom suite (`tests/run_isolated.py`) is green in the
default mode on that interpreter and on the equally optimized stock one
(the one pre-existing failure, `test_monkey_leak.py::test_no_leak_subprocess`,
is unrelated).  CPython's own stdlib suite passes on it (452 files, 46,657
tests).  Both migration probes under `tests/experiments/resume_rebuild/` are
10/10 clean.

With `RUNLOOM_MIGRATION=1` the suite is **not** green: 14 files fail or time
out.  Every failure reproduces identically on a non-optimized patched build, so
none of it is a PGO / LTO / tail-call effect, and none of it is the exec-home
patch.  They reduce to four root causes plus one class of expected semantic
differences.  Repro scripts: `tests/experiments/migration_stress/`.

## 1. SEGV at interpreter exit -- double clear of a per-g tstate

Deterministic (10/10, with `RUNLOOM_SYSMON=1` as the test sets it; ~1 in 3 runs
without sysmon) on the `heavy` auto-offload workload from
`tests/test_sysmon_oracle.py` (`monkey.patch(heavy=True)` + `sha256` of 8 MiB in
4 fibers on 4 hubs); 0/10 with migration off.  The process prints its result
and dies in `Py_FinalizeEx`:

```
llist_remove                              pycore_llist.h:83   node->prev == NULL
_Py_brc_remove_thread(per-g tstate)       brc.c:182
PyThreadState_Clear(per-g tstate)         pystate.c
runloom_g_decref                          runloom_sched_core.c.inc (Clear+Delete of g->tstate)
RunloomG_dealloc
merge_queued_objects                      brc.c:110
_Py_brc_remove_thread(hub tstate)         brc.c:177
PyThreadState_Clear(hub tstate)
Py_FinalizeEx
```

Finalization clears a hub thread's tstate, which merges that thread's
biased-refcount queue.  A `RunloomG` whose LAST reference was dropped on a
foreign OS thread (here a blockpool worker) sits in that queue, so its dealloc
runs here, and `runloom_g_decref` does `PyThreadState_Clear` on the fiber's
per-g tstate -- which CPython already tore down earlier in the same
finalization.  The second `_Py_brc_remove_thread` unlinks an already-unlinked
node.  Any per-g fiber object that dies on a foreign thread can arm this.

Fix direction: skip the Clear/Delete when the per-g tstate is no longer linked
(or when `_Py_IsFinalizing()`), or drain and delete every per-g tstate in
`runloom_mn_fini` before finalization gets to `zapthreads`.

## 2. The deadlock detector does not fire

`mn_sched_sysmon.c.inc` stands preemption down in per-g mode ("the hub's bound
tstate is DETACHED every per-g resume, so the ATTACHED-wedge arm never fires").
Preemption is what forces sysmon on by default; without it sysmon runs only
under an explicit `RUNLOOM_SYSMON=1`, and the deadlock census
(`runloom_mn_has_wakeable_work`) returns "wakeable" unconditionally while sysmon
is off -- so with the default environment `RUNLOOM_DEADLOCK=raise` can never
raise under migration.

Hangs: `test_cov_mn.py`, `test_mn_deadlock_detect.py`, `test_wait_reason.py`,
`test_cov_go_deadlock_differential.py`.  All four files pass under pytest with
`RUNLOOM_SYSMON=1 RUNLOOM_MIGRATION=1` (repeatedly, including `--noconftest`).

But sysmon is not the whole story.  The standalone probe
(`deadlock_detector_off.py`, the same recv-with-no-sender shape as the test)
raises in 0.05-0.08 s every time without migration, and under migration hung
in 30 of 32 attempts across `RUNLOOM_SYSMON=1` and `RUNLOOM_SYSMON=1
RUNLOOM_SYSMON_MS=20`; the two runs that did raise did so in 0.06-0.08 s.  So
once sysmon is on the census is *racy* under per-g mode rather than merely
gated: whatever quiescence signal it needs (`resume_start_ns` / the hub
`pending` fields, which sit at 1 on every hub in per-g mode) is usually never
satisfied.  Needs a real look at the census inputs under per-g rather than an
environment workaround.

## 3. `fiber_n` bulk-arena fibers never run

With `RUNLOOM_GON_BULK=1` (the bulk tests set it themselves) a `fiber_n` batch
under migration never completes: 32 fibers behind a `WaitGroup` hang forever,
the same script with the flag unset finishes in ~1 ms.  The bulk-arena batch
code in `mn_sched_init_fini.c.inc` never allocates per-g tstates (only the
single-spawn path does, near `PyThreadState_New(runloom_mn_interp)`), and the
hub loop in `mn_sched_hub_main.c.inc` treats `g->tstate == NULL` as "dead under
our claim" and skips the g.

Hangs: `test_spawn_bulk_lifecycle.py`,
`test_cov100_init_fini.py::test_fiber_n_bulk_wakes_idle_hubs`,
`test_swarm_mn_sched.py::test_fiber_n_bulk_path_indexed_integrity` and
`::test_large_fiber_n_set_equality_at_scale`.

Fix direction: allocate per-g tstates in the batch path (or refuse the bulk
path in per-g mode and fall back to the loop).

## 4. `select` competing with a direct `recv` hangs

`test_swarm_chan_select_sync.py::test_mn_select_competes_with_direct_recv_no_double_consume`
hangs 5/5 under migration and passes without; `RUNLOOM_SYSMON=1` changes
nothing, so it is not #2.  All fibers sit parked in `select` / `recv` / `send`
with no wake source: a lost wake in the select path when a select and a plain
recv contend for the same channel across hubs.  Not yet root-caused.

## Expected semantic differences (not bugs, but the tests assume otherwise)

Consequences of per-g mode standing preemption/sysmon down; the tests encode
the default-mode semantics:

- hub introspection shows every hub `detached`, `running_g=None`, `pending=1`
  (`test_hub_introspect.py`, `test_cov100_hubinfo_waitfd.py`);
- sysmon cannot classify an ATTACHED wedge
  (`test_sysmon_oracle.py::test_attached_cpu_loop_classified`);
- `WORLD_YIELD` never arms and the diagnostic ring never records `G_POP`
  (`test_cov95_diag.py`);
- spinners starve workers (`test_sched_fairness.py`).

Either gate these tests on `not runloom.migration_enabled()` or decide that
per-g mode should keep sysmon and preemption-equivalent fairness.

## Also observed

- A pyenv interpreter configured with `CPPFLAGS=-DPy_TSTATE_*` does NOT carry
  the defines in its installed `pyconfig.h` (only
  `tools/ci/build_patched_cpython.sh` appends them), so an extension built
  plainly against it reports `migration_status()` all False.  Build with
  `CFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME"`.
- Bench stress (default sizes, 5k units, 1M items) ran without crashes.  The
  1M-item 4-stage pipeline used 973 MB RSS with migration vs 23 MB without
  (~1 KB per queued item).
