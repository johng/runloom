# Workflow comparison: asyncio vs free-threaded threads vs runloom vs Go

Six workload *shapes* that show up in ordinary services, each written the
idiomatic way for four runtimes, driven by one script that runs every
(runtime, workload) pair in its own subprocess and prints a table of
median ops/s plus each runtime's ratio to Go.

| Column    | What it is |
| --- | --- |
| `asyncio`  | stock asyncio on the same free-threaded 3.14t interpreter, one OS thread, streams + `asyncio.Queue` + `gather` |
| `threads`  | one `threading.Thread` per concurrent unit, GIL **off**, blocking sockets, `queue.Queue`, `time.sleep` -- "just use threads" |
| `runloom`  | one fiber per concurrent unit on an M:N hub pool (`--workers` hubs), `Chan`, `WaitGroup`, `runloom.sync` sockets, `runloom.sleep` |
| `go`       | goroutine per unit, buffered channels, `sync.WaitGroup`, blocking `net.Conn` |

## Workloads (`common.py` is the single source of truth for sizes)

| Workload | Shape | ops |
| --- | --- | --- |
| `fanout_io`    | N clients, each K sequential TCP request/response round-trips to an echo server | N·K round-trips |
| `worker_pool`  | P producers -> bounded job queue -> W workers (small CPU step) -> results queue -> collector | jobs |
| `pipeline`     | 4-stage bounded-channel pipeline, json decode/mutate/encode per stage | items |
| `cpu_parallel` | N tasks each running a pure-Python integer loop; the core-scaling shape | loop steps |
| `sleepers`     | N tasks each sleeping S ms K times (waiting on a slow remote) | wakeups |
| `mixed`        | N handlers, each K × (round-trip, json transform, 1 ms "DB" sleep, round-trip) | handler iterations |

The network workloads all talk to the **same Go echo server** (`wf_go
-echoserver`), started once by the driver, so the backend is a fixed
target and never the thing being measured.

## Run

```sh
# from the repo root, extension built (python setup.py build_ext --inplace)
cd benchmark/workflows
PYTHON_GIL=0 ~/.pyenv/versions/3.14.4t/bin/python3.14 run_workflows.py
# subsets / sizes
... run_workflows.py --workloads fanout_io,mixed --runtimes runloom,go --n 2000
... run_workflows.py --samples 7 --warmup 2 --workers 8
```

Each cell = median of `--samples` runs after `--warmup` discarded runs;
the per-run dispersion is printed as %RSD.  Every run also reports its
resident set size before the workload and its peak afterwards; the driver
prints a second table of that delta (what the N concurrent units cost at
their high-water mark) and delta/N.  The driver also re-runs
`cpu_parallel` at `--workers 1` for runloom and Go (row `cpu_parallel@1`)
so the multi-core speedup is visible, not just the rate.  Output goes to
`results/workflows.json` with platform, interpreter, Go version and git
sha recorded.

A single runtime can be run by hand for profiling:

```sh
PYTHON_GIL=0 PYTHONPATH=../../src python wf_runloom.py --workload pipeline --workers 8
(cd wf_go && go run . -workload pipeline)
```

## Reading the numbers honestly

- **Cross-language absolutes on `cpu_parallel`, `worker_pool` and
  `pipeline` are mostly interpreter speed.**  Go runs the same integer
  loop ~40x faster per step than CPython; that gap is the language, not
  the scheduler.  The scheduler story is in the *ratios within Python*
  (asyncio vs threads vs runloom) and in `cpu_parallel` vs
  `cpu_parallel@1` (core scaling).
- **The CPU kernel is a pure-Python LCG, not `hashlib.sha256`, on
  purpose.**  sha256 does not scale on a free-threaded build (OpenSSL's
  digest-fetch path serializes: 8 threads ran at 0.85x of one on this
  box), which made every runtime look single-core.  If you change the
  kernel, check it scales under plain threads first.
- **Network workloads run over loopback** against a Go echo server; on a
  Linux host with an nft ruleset loopback pays ~14% (see the repo
  `CLAUDE.md`), so compare rows within one machine only.
- `threads` at high N is dominated by thread creation and kernel
  scheduling; that is the real cost of that model, not a harness artifact.
  `WF_THREAD_STACK` (default 256 KiB) sets the per-thread stack.
- `asyncio` runs CPU work inline on the loop thread.  Offloading to an
  executor would measure a thread pool, not asyncio.
- **Memory is peak RSS delta, so it includes fixed runtime costs.**  For
  runloom that is the per-hub stack pool and the hub threads themselves,
  which at small N (cpu_parallel, 64 fibers) dominate the per-unit figure.
  Read bytes/unit at the large-N rows (`sleepers`, `fanout_io`); for
  `worker_pool` / `pipeline` N is an item count, so the per-unit figure
  there is per queued item, not per task.  Only touched pages count, so a
  256 KiB thread stack reserve shows up as the ~100-200 KiB actually used.
- **Interpreter build matters and is recorded in the JSON.**  The stock
  3.14.4t here is PGO + thin LTO + tail-calling interpreter
  (`--enable-optimizations --with-lto=thin --with-tail-call-interp`); the
  exec-home variants (`3.14.4t-{orig,mig,halfa}`) were rebuilt the same way
  on 2026-09-14 so an A/B against them is not confounded by build quality.

## Results (2026-09-14, Apple M-series 18-core, 128 GB, 3.14.4t PGO+LTO+tailcall, go1.26)

Columns: `asyncio` (1 thread), `threads` (stdlib bounded `queue.Queue`),
`threads-sq` (C `queue.SimpleQueue`, unbounded), `runloom` (18 hubs, no
migration), `runloom-mig` (patched interpreter, cross-hub migration on), `go`
(GOMAXPROCS=18).  Ratios are runloom-mig / threads and / asyncio unless noted.
Raw tables: `results/*.json`; re-print any with `python report.py <file>`.

### Baseline, default sizes, median of 3 (`results/workflows.json`)

```
ops/s                  asyncio    threads    runloom  runloom-mig         go   mig/threads  mig/asyncio
fanout_io               137.1k     135.2k     155.0k      140.9k     156.5k      1.04         1.03
worker_pool             108.4k      44.0k     275.6k      382.9k      3.05M      8.70         3.53
pipeline                132.7k     163.0k     178.5k      313.2k     863.4k      1.92         2.36
cpu_parallel            23.77M    216.47M    188.10M     182.63M     31.93G      0.84         7.68
sleepers                501.2k      95.0k     391.3k      368.2k     891.3k      3.88         0.73
mixed (10us cpu)         36.0k      47.4k      58.2k       58.1k      69.6k      1.23         1.61
spawn_churn (100k)      264.5k      16.5k       5.3k        6.5k      5.00M      0.39         0.02
```

### Where runloom beats threads AND asyncio

**Request handlers with real CPU** (`results/hubs_mixed.json`, 2000 handlers,
~200 us of Python per request + 1 ms fake DB wait + two round-trips):

```
hubs      asyncio  threads  runloom  runloom-mig   vs threads  vs asyncio
1            5.3k     9.2k     5.1k        5.1k        0.55        0.96
4                             17.3k       14.7k        1.61        2.77
8                             22.9k       25.2k        2.74        4.75
18                            29.7k       42.4k        4.61        8.00
```
asyncio is pinned to one core; 2000 OS threads are pinned at 9k by kernel
scheduling; runloom-mig scales 8.3x to 18 hubs at a tenth of the thread memory
(60 KB vs 660 KB per handler).  At 200 handlers threads still win the heavy-CPU
rows (39.5k vs 39.0k at 200 us, 12.7k vs 10.8k at 1 ms): the crossover is
between 200 and 2000 concurrent handlers.  This is the headline shape.

**Pipeline** (4 bounded stages, `results/handoff_pipeline.json`, 500k items):
runloom-mig 319k vs threads-sq 272k (1.17x), threads 174k, asyncio 132k.
Without migration runloom does NOT scale on a pipeline (a 4-stage chain has
at most 4 runnable fibers, each pinned to its home hub): 178k, below the C
queue.  Migration is what makes it win, and it plateaus at 4 hubs like Go.

### Where the first-cut win was the queue, not the runtime

**Worker pool** (`results/handoff_worker_pool.json`, 200k jobs, job=0 is
pure hand-off, job=200 is ~8 us of Python per job):

```
                asyncio  threads  threads-sq  runloom  runloom-mig      go
hand-off only     1.08M    71.5k       1.35M   561.4k       545.6k   4.51M
with cpu step    110.9k    40.0k      529.4k   360.6k       410.3k   2.95M
```
The 10x over `threads` is the pure-Python `queue.Queue`; with the C
`SimpleQueue` free threads hand off 2.4x faster than runloom's `Chan` and
finish 1.3-1.5x ahead with real work.  runloom still beats asyncio 3.3-3.7x
(one core) and its channel is bounded (back-pressure) where SimpleQueue is
not.  Report this as "beats asyncio; competitive with C-queue threads".

### Scaling with hubs (`results/hubs_*.json`)

```
                        1 hub    2      4      8     18    threads   asyncio
worker_pool  runloom     120k  220k   374k   395k   333k      41k      114k
             runloom-mig 116k  220k   369k   446k   420k
cpu_parallel runloom      24M   47M    84M   115M   214M     241M       24M
             runloom-mig  24M   44M    80M   127M   215M
```
Channel-heavy work peaks at 8 hubs (one bounded channel, 40 contenders; Go
declines with procs on the same shape).  Pure CPU scales 8.8x on 18 hubs
(Go 15x); at 10k tasks runloom-mig reaches threads parity (244M).

### Limits (`results/limit_*.json`)

- **Volume is fine**: worker_pool and pipeline are flat from 200k to 2M items.
- **Fan-out** (`sweep_fanout.json`): runloom matches Go and beats threads
  from 2k clients on (1.6x at 2k, 2.0x at 5k; 52 KB vs 613 KB per connection).  Above
  ~10k every multi-threaded runtime incl. Go collapses on macOS loopback
  while single-threaded asyncio holds; macOS also caps ~16k ephemeral ports
  per local IP, so higher N needs Linux.
- **Sleepers** (`sweep_sleepers.json`, `limit_sleepers.json`): runloom beats
  threads at every N (4x at 5k, 10x at 100k) but falls behind asyncio above
  ~20k: 35 KB per parked fiber -> 16.5 GB at 500k vs 880 MB for asyncio,
  and throughput tracks the working set (88k vs 470k wakeups/s at 500k).
- **Spawn churn is a scheduler defect** (`hubs_spawn.json`, `limit_spawn*.json`):
  spawn rate FALLS with hubs (124k/s at 1 hub -> 14k at 18; mig 131k -> 6.5k)
  and with N (34k at 10k tasks -> 4.5k at 1M, 3x slower than creating OS
  threads).  The spawner outruns the drain so every pending fiber holds a full
  stack (1.7 GB per 100k at 1 hub, 3.4 GB at 18, 11.6 GB with mig) and
  cross-hub stack allocation is contended.  asyncio: flat 260k/s.

### Linux (DigitalOcean c-8, Xeon Platinum 8358, 8 vCPU / 16 GB, Ubuntu 24.04, clang 19, go1.27; `results/linux_*.json`)

Same code, same flags (both interpreters PGO+thin LTO+tail-call, `runloom-mig`
against the patched build).  8 vCPU is 4 physical cores with SMT, so ~4x is
the real ceiling for CPU work.

```
ops/s (baseline)       asyncio    threads    runloom  runloom-mig         go   mig/threads  mig/asyncio
fanout_io                58.8k    INVALID     207.2k      148.7k     322.3k       -            2.5
worker_pool              47.1k      38.3k     157.3k      149.5k      2.23M      3.9          3.2
pipeline                 45.5k      57.6k      80.8k      103.4k     342.5k      1.8          2.3
cpu_parallel             9.88M     35.63M     35.15M      32.74M     10.93G      0.92         3.3
sleepers                201.1k      71.4k     302.4k      216.0k     813.2k      3.0          1.1
mixed (10us cpu)         16.2k      20.0k      35.5k       31.3k      93.1k      1.6          1.9
```

- **The Linux `threads` fan-out and mixed cells are INVALID** (1.4-2.2M
  round-trips/s is impossible for Python): the threads implementation marked
  the timed start from the main thread AFTER the barrier released, and with
  thousands of runnable threads on 8 cores main was descheduled first, shrinking
  the window.  Fixed (the barrier's last arrival marks start, before release);
  the macOS threads fan-out/mixed cells were inflated 15-30% by the same bug
  and were regenerated.  The Linux box was deleted before a rerun; every other
  Linux column is unaffected (they mark before release).
- **The c-8 `runloom-mig` column ran with asserts live.**  Its extension was
  built with `CFLAGS=-D...` in the environment, which in setuptools REPLACES
  the interpreter's base flags, dropping `-DNDEBUG`.  Read that column as a
  lower bound.  Pass the defines through `RUNLOOM_EXTRA_CFLAGS` instead (see
  `setup.py`); the c-32 rerun below does.
- **`runloom-mig` segfaults in `run()` teardown on the two socket workloads
  (`mixed`, `fanout_io`) on Linux x86-64**, deterministically, at 1, 8 and 32
  hubs, with either revision of the exec-home patch; the five non-socket
  workloads pass.  macOS does not reproduce it.  Write-up with backtraces and
  hypotheses: `docs/dev/mig-mixed-teardown-crash.md` on branch
  `docs/mig-mixed-teardown-crash`.  Until it is fixed the Linux mig cells for
  those two workloads are missing and the mig `sleepers` cell (9x slower than
  stock, 165 KB/fiber) is suspect.
- **Sleepers**: runloom beats BOTH threads (2.7x) and asyncio (1.5-2.1x) at every
  N to 100k on Linux -- asyncio's epoll timer path is 2.6x slower than on macOS
  (200k vs 530k wakeups/s) while runloom holds 300-420k.
- **Mixed with real CPU** is where Linux disagrees with macOS: at ~400 us of
  Python per request (cpu=4000 on this slower core) runloom gets 6.2-7.6k/s
  vs threads 10.6-16.8k/s and migration does NOT help (6.8-6.9k); parallel
  efficiency ~31% vs 84% for threads.  On macOS migration doubled this shape.
  The non-migrating wake-placement problem is real on both; whatever makes
  migration rescue it on Darwin does not carry to Linux/epoll.
- **cpu_parallel**: parity with threads (35.2M vs 35.6M, 3.6x on 8 vCPU).
- **Fan-out vs Go**: runloom 0.64x Go on Linux (207k vs 322k) where macOS is 1.0x;
  Go's epoll netpoller is the better one here.  runloom-mig is 0.7x runloom.
  Sweep to 50k connections (`linux_sweep_fanout.json`): runloom 211k -> 141k,
  Go 358k -> 220k, no cliff (the macOS 10k cliff is Darwin-specific).

### Linux, 32 vCPU (DigitalOcean c-32, 2026-09-15; `results/linux32_*.json`)

Rerun of the suite on a 32 dedicated-vCPU box with the fixed threads barrier,
identical extension flags for stock and mig, and hub-scaling rows to 32.
Tables are filled in from the `linux32_*.json` files as they land; re-print
any with `python report.py --baselines threads,asyncio results/linux32_<name>.json`.

### Interpreter builds

- `runloom`: stock 3.14.4t, migration unavailable (a parked fiber resumes on
  its home hub).  `runloom-mig`: `~/.pyenv/versions/3.14.4t-mig` built with
  both `src/patches`, extension compiled against it with the same defines
  (`build/lib-mig`); the driver refuses to label a run runloom-mig unless the
  process reports migration active.  All four local interpreters were rebuilt
  2026-09-14 with `--enable-optimizations --with-lto=thin
  --with-tail-call-interp` (halfa: no LTO, its call-form patch is not LTO-safe).
- Thin LTO needs `__attribute__((used))` on `_Py_tss_tstate` with the exec-home
  patch (fix on the exec-home branch / PR #10).

### Open items the numbers point at

1. Spawn path: per-fiber stack allocation contended across hubs; spawner
   should not outrun the drain (or stacks should be lazier/smaller).
2. Per-fiber memory (35-53 KB parked, 18 KB minimum) is the gap to asyncio on
   idle-heavy work and the cause of the sleepers/spawn cliffs.
3. Channel hand-off at ~450 ns/op is half the C SimpleQueue; the collector
   fiber serializes the worker pool above 8 hubs.
4. Non-migrating wake placement leaves cores idle on CPU-heavy handlers
   (31% efficiency vs 65% with migration); migration costs 2.4x memory.
