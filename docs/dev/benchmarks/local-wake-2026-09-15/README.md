# Local-wake workflow comparison (2026-09-15)


## Findings

**Local wake helps some workflows, but is not an across-the-board speedup.**
The strongest repeated result is at eight hubs: worker-pool throughput rises
**10.8–12.9%**, and mixed-I/O throughput rises **6.7–10.5%** across two batches.
At 18 hubs the repeated increases are smaller: worker pool **3.3–7.1%**,
mixed I/O **4.1–4.5%**. These ranges describe the two batch medians, not
confidence intervals. The workflow suite does not establish a general 2x gain.

Tradeoffs and uncertainty:

- At two hubs, mixed I/O is **7.2–9.2% slower** in both batches. That is a
  repeatable counterexample to a universal performance benefit.
- TCP fan-out improved **4.2–12.0%** in the initial sweep; it was not included
  in the confirmation batch, and the 18-hub gain is close to sample variation.
- Timers initially fell **42.5% / 12.8% / 2.6%** at 2 / 8 / 18 hubs, then
  changed to **+6.3% / +8.4% / +3.0%** in confirmation. At two hubs the latter
  batch has **26–35% MAD**. Timer performance is **inconclusive**, not a
  confirmed regression or benefit; the raw batches must both be retained.
- Task creation fell **16.8% at eight hubs** in the first sweep, with
  **7.4% MAD** for local wake. At two hubs the difference was **-2.3%** and
  at 18 hubs **+1.9%**. This workload was not repeated, so the eight-hub result
  is a regression signal requiring follow-up rather than an established cost.
- Pipeline and CPU results are small or variable; no broad benefit is shown.

All **432 subprocesses** across both batches (372 measured, 60 warmups)
completed successfully and reported active migration. This supports these
workloads' completion and teardown on this host, not comprehensive scheduler
correctness. See validation below.

## Method

Host: Apple M5 Max, 18 logical CPUs, 128 GiB RAM; Go 1.26.6. Normal
desktop applications remained running; no other heavy benchmark was observed
in a process-load spot check. Alternation reduces drift but does not make
this a dedicated benchmark host.

Compare `de94a1e5` (parent of the local-wake change) against `3a71b576`
(local wake), with exec-home commit `55d25600` applied to **both** source
snapshots. The runs themselves were made from the pre-squash commits
`f97919cf` and `2863684d`, which the JSON labels record; their `src/`
trees are byte-identical to `de94a1e5` and `3a71b576` respectively
(`git rev-parse <sha>:src`), so the reachable commits are named here. This isolates the scheduler change, including its default-mode
routing decisions, from interpreter changes. Both benchmark columns run
with migration enabled. This is not a comparison against older published
workflow results, whose scheduler source differs.

The seven unmodified workloads and helper functions come from
`bench/workflow-compare` commit `75dd06e9`. `compare.py` changes only the
orchestration: one warmup and five measured subprocesses per variant and
cell, alternating baseline/local order, at 2, 8, and 18 hubs. Sizes are the
harness defaults. Every subprocess must exit successfully and report active
migration before its throughput counts. A shared Go echo server provides
the network endpoint; the file-descriptor limit is raised as in the original
runner. Each process has a 45-second timeout. Warmups remain in the raw data
with `sample: -1` and are excluded from summaries.

The same installed CPython 3.14.4t-mig interpreter runs both extensions:
PGO, ThinLTO, tail-call interpreter, `PYTHON_GIL=0`, both allocation-home and
execution-home defines enabled. Extensions are fresh release builds, using
`RUNLOOM_EXTRA_CFLAGS` to append the two defines without losing `-DNDEBUG`.
No coverage instrumentation is used for throughput measurements.

Interpreter provenance: installed `pycore_pystate.h` has `_Py_tstate_tls_read`;
the build source at `/Users/johng/projects/cpython-mig` also routes
`current_fast_get()` through it. The allocation patch passes a reverse
application check. The execution patch passes a reverse application check
on copied source after normalizing one difference: this installed build
marks `_Py_tss_tstate` `used` unconditionally, whereas `55d25600` guards that
attribute with `_Py_HAVE_TSTATE_TLS_READ`. That condition is true for this
arm64 Darwin build, so the tested behavior is the same. The interpreter was
reused, not rebuilt from the consolidated patch during this experiment.

`results.json` retains every subprocess's rate, elapsed time, RSS, extension
path, migration status, and errors, plus interpreter configuration and source
identities. Measurements cover this macOS arm64 host only. They do not
establish Linux performance or resolve previously documented Linux teardown
failures. Peak RSS is process high-water growth, not per-fiber allocation.

## Reproduce

From a runloom checkout containing the three commits above, prepare isolated
snapshots (the directory below should be new/empty):

```sh
export LOCAL_WAKE_AB_ROOT=/private/tmp/runloom-local-wake-ab
mkdir -p "$LOCAL_WAKE_AB_ROOT"/{base,local,harness}
git archive de94a1e5 | tar -x -C "$LOCAL_WAKE_AB_ROOT/base"
git archive 3a71b576 | tar -x -C "$LOCAL_WAKE_AB_ROOT/local"
git archive 75dd06e9 benchmark/workflows | tar -x -C "$LOCAL_WAKE_AB_ROOT/harness"
git show 55d25600 --format= -- src/patches | git -C "$LOCAL_WAKE_AB_ROOT/base" apply
git show 55d25600 --format= -- src/patches | git -C "$LOCAL_WAKE_AB_ROOT/local" apply
export MIG_PY="$HOME/.pyenv/versions/3.14.4t-mig/bin/python3.14"
for variant in base local; do
  (
    cd "$LOCAL_WAKE_AB_ROOT/$variant"
    RUNLOOM_EXTRA_CFLAGS='-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME' \
      "$MIG_PY" setup.py build_ext -f --build-lib build/lib-mig --build-temp build/temp-mig
  )
done
(cd "$LOCAL_WAKE_AB_ROOT/harness/benchmark/workflows/wf_go" && go build -o wf_go .)
"$MIG_PY" docs/dev/benchmarks/local-wake-2026-09-15/compare.py
```

The benchmark needs permission to listen on loopback TCP. Keep unrelated
CPU-intensive work off the host during measurements. `MIG_PY` must carry
both CPython patches; a stock free-threaded interpreter is insufficient.

## Full sweep

Median throughput of five samples. MAD is median absolute deviation divided
by the median; it describes dispersion, not a confidence interval. Small
changes comparable to dispersion should not be treated as proven gains.

| Hubs | Workload | Baseline ops/s | Local ops/s | Change | MAD % (base/local) |
| ---: | --- | ---: | ---: | ---: | ---: |
| 2 | fanout_io | 152,657 | 170,984 | +12.0% | 1.3/1.8 |
| 2 | worker_pool | 214,909 | 222,346 | +3.5% | 2.4/1.0 |
| 2 | pipeline | 197,652 | 197,838 | +0.1% | 0.1/0.4 |
| 2 | cpu_parallel | 45,744,329 | 44,942,403 | -1.8% | 0.1/1.9 |
| 2 | sleepers | 219,151 | 126,015 | -42.5% | 5.7/4.6 |
| 2 | mixed | 56,026 | 50,850 | -9.2% | 2.5/2.4 |
| 2 | spawn_churn | 4,417 | 4,317 | -2.3% | 6.1/14.5 |
| 8 | fanout_io | 137,633 | 147,003 | +6.8% | 1.6/1.3 |
| 8 | worker_pool | 398,401 | 449,937 | +12.9% | 3.5/2.3 |
| 8 | pipeline | 322,500 | 317,102 | -1.7% | 0.3/1.2 |
| 8 | cpu_parallel | 110,469,999 | 114,391,893 | +3.6% | 1.3/2.1 |
| 8 | sleepers | 457,441 | 399,026 | -12.8% | 3.6/3.3 |
| 8 | mixed | 53,963 | 59,626 | +10.5% | 2.4/2.9 |
| 8 | spawn_churn | 6,982 | 5,808 | -16.8% | 1.5/7.4 |
| 18 | fanout_io | 139,713 | 145,637 | +4.2% | 2.6/3.0 |
| 18 | worker_pool | 362,853 | 374,953 | +3.3% | 2.1/2.7 |
| 18 | pipeline | 299,237 | 304,227 | +1.7% | 2.3/3.3 |
| 18 | cpu_parallel | 180,995,528 | 172,020,179 | -5.0% | 6.3/3.1 |
| 18 | sleepers | 372,345 | 362,845 | -2.6% | 0.9/0.3 |
| 18 | mixed | 56,732 | 59,258 | +4.5% | 1.9/2.4 |
| 18 | spawn_churn | 7,014 | 7,150 | +1.9% | 2.2/1.6 |

## Independent confirmation

After the full sweep, repeat the worker-pool, timer, and mixed workloads with
nine measured samples per variant and one warmup. Same sizes, interpreter,
flags, alternation, and hardware. This is a separate batch; do not merge its
samples with the first batch to hide between-batch variation.

```sh
"$MIG_PY" docs/dev/benchmarks/local-wake-2026-09-15/compare.py \
  --workloads worker_pool,sleepers,mixed --samples 9 \
  --out "$LOCAL_WAKE_AB_ROOT/confirmation.json"
```

| Hubs | Workload | Baseline ops/s | Local ops/s | Change | MAD % (base/local) |
| ---: | --- | ---: | ---: | ---: | ---: |
| 2 | worker_pool | 208,640 | 214,493 | +2.8% | 0.5/1.1 |
| 2 | sleepers | 155,513 | 165,302 | +6.3% | 26.4/34.7 |
| 2 | mixed | 55,698 | 51,696 | -7.2% | 4.1/1.5 |
| 8 | worker_pool | 408,409 | 452,444 | +10.8% | 2.0/1.2 |
| 8 | sleepers | 410,039 | 444,633 | +8.4% | 15.3/5.6 |
| 8 | mixed | 53,694 | 57,277 | +6.7% | 3.1/1.8 |
| 18 | worker_pool | 360,334 | 386,016 | +7.1% | 1.7/2.1 |
| 18 | sleepers | 349,690 | 360,229 | +3.0% | 2.6/1.7 |
| 18 | mixed | 55,770 | 58,070 | +4.1% | 1.8/2.3 |

## Validation

Both builds passed the same four test files under migration, with one pytest
process per file: `test_chan.py` (26), `test_hub_pinning.py` (7),
`test_offload_hubs.py` (11), `test_freethread_stress.py` (5): **49 tests per
variant**, no failures. In addition, the local build passed
`test_local_wake.py` (4 tests) in **20 fresh processes**, 80 passing test
executions. These are release builds; the coverage-counter assertions in
`test_local_wake.py` are conditional and were not exercised.

Example invocation, from either source snapshot:

```sh
PYTHON_GIL=0 RUNLOOM_MIGRATION=1 PYTHONPATH=build/lib-mig:src \
  "$MIG_PY" -m pytest tests/test_chan.py -q
```

Raw pytest output and exit statuses are in `tests.json`. The two exec-home
patch files also passed `git apply --numstat` parsing checks. No claim is
made here about the entire test suite, default non-migration mode, CPython's
full test suite, or other operating systems.

The requested exec-home commit `55d25600` is cherry-picked onto this branch
(first in the sequence); all three files under `src/patches` match it exactly.
The original benchmark worktree's uncommitted runner/provenance changes were
left intact. The experiments used archived tracked source, not those edits.

## Follow-up (2026-09-16): why mixed loses at 2 hubs, batch steal, pump cadence

Same host, same `3.14.4t-mig` interpreter, `RUNLOOM_MIGRATION=1`. These runs
use `-DRUNLOOM_COVER` builds (the counters are the point), the harness's
`wf_runloom` workload functions driven by `followup-2026-09-16/mixed_probe.py`,
one run per line, three alternating repetitions. Absolute rates are a little
below the release-build tables above; compare within a block only. Raw
output and the scripts are in `followup-2026-09-16/`.

**Cause of the 2-hub mixed loss.** Nearly every wake in `mixed` is made by
the netpoll pump (two socket parks per iteration; the 1 ms sleep is a timer
and goes to the owner's ready ring). Under local wake the pump's wakes land
on the pumping hub's deque, and, because each fiber then re-arms its socket
on the hub it ran on, the busy hub ends up owning most parkers in its
**private kqueue**. A busy hub only drains that kqueue every 64 pick steps
(`self_pump_ctr & 0x3f` in `hub_main`); an idle hub drains it at once. With
the global run-queue, fibers hop hubs on every wake, so the parkers are spread
across both kqueues and the idle hub's blocking pump serves half of them with
no added latency.

Two experiments (scratch builds, not committed) confirm it. Routing pump
wakes to the global queue (`patch_exp.py`, Go's `injectglist`) restores the
2-hub number and forfeits the 8-hub gain. Changing the busy-hub self-pump
cadence (`patch_cad.py`, `RUNLOOM_SELF_PUMP_MASK`) does both:

| Hubs | Build | mixed ops/s (3 runs) | median |
| ---: | --- | --- | ---: |
| 2 | baseline (global queue) | 52.8k / 58.8k / 57.2k | 57.2k |
| 2 | local wake + batch steal, cadence 64 | 53.3k / 51.9k / 52.0k | 52.0k |
| 2 | same, cadence 16 | 59.6k / 55.9k / 61.3k | **59.6k** |
| 2 | same, cadence 4 | 51.8k / 47.3k / 48.2k | 48.2k |
| 2 | same, every turn | 29.5k / 29.7k / 29.2k | 29.5k |
| 8 | baseline (global queue) | 52.4k / 57.2k / 54.8k | 54.8k |
| 8 | local wake + batch steal, cadence 64 | 60.0k / 59.3k / 64.0k | 60.0k |
| 8 | same, cadence 16 | 63.3k / 64.2k / 69.6k | **64.2k** |
| 8 | same, cadence 4 | 39.3k / 56.4k / 54.6k | 54.6k |
| 8 | same, every turn | 42.3k / 42.6k / 46.6k | 42.6k |

Cadence 16 beats the baseline on both hub counts; 4 and 1 pay a `kevent`
syscall per few pick steps and lose. The cadence is not changed on this
branch: it is compiled only on the per-hub-kqueue backend (Darwin), it
affects default mode too, and it needs the release-build sweep at 18 hubs
and the fan-out shapes before it moves. It is the next thing to try.

**Batch steal** (`3a71b576` + this branch's steal-half commit, build `half`,
versus `new` = local wake with single-item steal and `base2` = global queue).
The thief takes up to half of the victim's deque per pick step, as repeated
CAS-validated single-item steals (the deque protocol is unchanged; see
`CLAUDE.md`). `steal_batch` counts the extras. Medians of three:

| Hubs | Workload | base2 | new (steal 1) | half (steal ½) | extras/steal (half) |
| ---: | --- | ---: | ---: | ---: | ---: |
| 2 | mixed | 57.2k | 51.6k | 52.0k | ~2 |
| 2 | worker_pool | 219.4k | 225.3k | 213.9k | ~7 |
| 2 | fanout_io | 160.5k | 176.0k | 177.2k | ~10 |
| 2 | sleepers | 136.8k | 181.5k | 221.2k | ~30 |
| 8 | mixed | 54.8k | 58.0k | 60.0k | ~2 |
| 8 | worker_pool | 426.7k | 453.2k | 460.5k | ~2 |
| 8 | fanout_io | 135.7k | 146.7k | 144.1k | ~6 |
| 8 | sleepers | 468.0k | 441.2k | 446.3k | ~1.3 |

Read this as neutral to slightly positive: fan-out and mixed at 8 hubs move
a few percent, sleepers at 2 hubs moves a lot but its run-to-run spread is
26-35% (see the confirmation batch above), and worker_pool at 2 hubs is
within noise. The pump's batches in `mixed` are ~2 fibers, which is why
halving them changes nothing there. Batch steal is kept because it is the
right mechanism for real batches (bulk spawn, a 256-fiber wake burst: see
`tests/test_steal_batch.py`) and costs nothing when batches are small.

Validation for the batch-steal commit: `tests/test_steal_batch.py` (3 tests)
in default mode on stock 3.14.4t and in migration mode on the cover build;
the default-mode subset (`test_mn`, `test_chan`, `test_offload_hubs`,
`test_hub_pinning`, `test_local_wake`, `test_steal_batch`,
`test_swarm_mn_sched`, `test_freethread_stress`, `test_differential_asyncio`,
`test_tlbc_parked_frame_gc`) via `tests/run_isolated.py`; the migration-mode
subset on the release `build/lib-mig` with the deadlock tests deselected
(pre-existing hang, see `docs/dev/MIGRATION_DEFAULT_ANALYSIS.md` 0b);
`tools/verify/model_source_drift.py` (the anchored `runloom_cldeque_steal`
is untouched). Results are recorded in the commit message.
