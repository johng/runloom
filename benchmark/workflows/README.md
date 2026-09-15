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

### Linux, 32 vCPU (DigitalOcean c-32, Xeon Platinum 8280, 32 cores no SMT, 64 GB, Ubuntu 24.04, kernel 6.8, clang 19, go1.27; `results/linux32_*.json`, 2026-09-15)

Rerun with the fixed threads barrier and identical extension flags for stock
and mig (`RUNLOOM_EXTRA_CFLAGS`), 32 hubs / GOMAXPROCS=32.  The box was
deleted before the suite finished; groups that ran are listed with their
files, the rest are noted at the end.  `runloom-mig` crashed (`exit -11`) on
every `fanout_io` and `mixed` cell and is 5-20x slower than stock on
`sleepers`, so the ratios below are **stock runloom** (no migration) vs
threads and asyncio.  Provenance (runloom commit, tree hashes, build flags):
`results/PROVENANCE.md`.

Baseline, default sizes, median of 3 (`linux32_workflows.json`):

```
ops/s          asyncio  threads  threads-sq  runloom  runloom-mig       go   runloom/threads  runloom/asyncio
fanout_io        78.0k   311.7k     311.3k   388.4k     exit -11   861.1k      1.25             4.98
mixed            14.6k    24.3k      24.9k    61.9k     exit -11    68.0k      2.54             4.24
worker_pool      42.3k    52.1k     365.1k   145.3k       335.4k    1.76M      2.79 (0.40 sq)   3.44
pipeline         43.5k    83.1k     109.2k    83.2k        78.2k   384.7k      1.00 (0.76 sq)   1.91
cpu_parallel     8.44M  106.09M    104.99M   97.50M      105.23M   18.38G      0.92            11.55
cpu_parallel@1       -        -          -    8.60M        8.74M    3.23G      (11.3x on 32 hubs)
sleepers        186.2k    87.9k      90.1k   297.9k        33.4k   811.4k      3.39             1.60
spawn_churn      90.1k     7.8k       7.7k    10.4k          999    2.44M      1.34             0.12

peak RSS/unit   asyncio  threads  runloom  runloom-mig
fanout_io          11k     238k      63k     -
mixed              17k     250k     133k     -
sleepers            2k      25k      18k    165k
spawn_churn        928      635      14k     36k
```

Fan-out sweep, 16 echo ports (`linux32_sweep_fanout.json`):

```
round-trips/s   asyncio  threads  runloom       go   runloom/threads  runloom/asyncio  runloom/go
n=500             70.6k   303.2k   387.1k   757.4k       1.3             5.5           0.51
n=2000            68.4k   234.7k   512.1k    1.13M       2.2             7.5           0.45
n=5000            52.8k   169.6k   467.5k   964.4k       2.8             8.9           0.48
n=10000           50.1k   122.3k   433.3k   867.0k       3.5             8.6           0.50
n=20000           52.1k    83.0k   392.2k   809.0k       4.7             7.5           0.48
n=50000           52.0k    59.7k   338.9k   716.4k       5.7             6.5           0.47
```

Sleepers sweep (`linux32_sweep_sleepers.json`):

```
wakeups/s       asyncio  threads  runloom  runloom-mig       go   runloom/threads  runloom/asyncio
n=5000           171.2k    85.9k   304.4k        56.3k   809.7k       3.5             1.8
n=20000          172.5k    83.5k   346.6k        24.7k    2.41M       4.2             2.0
n=50000          166.2k    77.4k   229.2k        19.6k    3.51M       3.0             1.4
n=100000         158.8k    75.1k   219.8k        12.9k    4.34M       2.9             1.4
n=200000         156.7k    74.3k   217.3k        11.5k    4.24M       2.9             1.4
```

- **Fan-out is the clearest Linux win.**  Threads degrade with connection
  count while runloom holds 340-510k round-trips/s, so the margin widens from
  1.3x at 500 connections to 5.7x at 50k; 5.5-8.9x over asyncio throughout.
  Go stays ~2x ahead of runloom at every size (0.64x on c-8, 1.0x on macOS):
  Go's epoll netpoller scales with cores better than runloom's.
- **Mixed** (10 us CPU, 200 handlers): stock runloom 2.5x threads, 4.2x asyncio,
  0.91x Go.  On c-8 it was 1.6x threads; more cores widen the gap.
- **Sleepers**: runloom beats threads 2.9-4.2x and asyncio 1.4-2.0x at every N
  to 200k with no cliff (macOS falls behind asyncio above 20k).  Throughput
  peaks at 20k then settles at ~220k.
- **cpu_parallel**: parity with threads (0.92x at 64 tasks, 0.97x at 1024 from
  the partial cpu-sweep log), 11.3x over one hub on 32 cores: neither threads
  nor runloom get past ~114M steps/s, so the ceiling here is the free-threaded
  interpreter, not the scheduler.
- **worker_pool / pipeline**: runloom's Chan is 2.8x the pure-Python bounded
  `queue.Queue` and 0.4x the C `SimpleQueue`; pipeline is a wash with threads.
- **Memory**: runloom is a quarter of threads' RSS per connection on fan-out
  (63 vs 238 KB) and 18 vs 25 KB per sleeper; asyncio is 2-17 KB per unit.
- **runloom-mig on Linux is broken in this snapshot**: teardown segfault on the
  socket workloads, sleepers collapsing with N (0.65x threads at 5k to 0.15x at
  200k, 165 KB/fiber), spawn churn at 1k/s.  Only worker_pool (335k, 2.3x stock)
  and cpu_parallel look healthy.  See `docs/dev/mig-mixed-teardown-crash.md`.
- **Spawn churn** is unchanged: 10k spawns/s at 100k tasks vs Go 2.4M.

Mixed sweep, 8 echo ports, ratios stock runloom / threads and / asyncio
(`linux32_sweep_mixed.json`; `cpu=` is LCG iterations per request, roughly
10 us / 200 us / 1 ms of Python on this core):

```
handler iters/s     asyncio  threads  runloom       go   runloom/threads  runloom/asyncio  runloom/go
n=200   cpu=200       14.7k    24.8k    63.1k    70.2k       2.5             4.3           0.90
n=200   cpu=4000       1.9k    10.7k    16.3k    75.7k       1.5             8.6           0.22
n=200   cpu=20000      431      4.1k     4.1k    60.8k       1.0             9.5           0.07
n=2000  cpu=200       14.1k    20.2k   130.7k   304.5k       6.5             9.3           0.43
n=2000  cpu=4000       1.9k    10.6k    23.2k   259.9k       2.2            12.2           0.09
n=2000  cpu=20000      420      4.0k     5.2k   247.3k       1.3            12.4           0.02
n=10000 cpu=200       13.0k    15.0k   146.8k   325.1k       9.8            11.3           0.45
n=10000 cpu=4000       1.9k     9.8k    24.9k   314.2k       2.5            13.1           0.08
n=10000 cpu=20000      430      3.9k     5.2k   284.6k       1.3            13.4           0.02
```

- **Concurrency is where runloom pulls away**: at 10 us of CPU per request the
  margin over threads grows from 2.5x at 200 handlers to 9.8x at 10k, and
  runloom is the only Python runtime whose throughput RISES with handler count
  (63k -> 147k) while threads fall (25k -> 15k).
- **CPU-heavy handlers are the weak spot, on Linux as on c-8**: at 1 ms of
  Python per request runloom only ties threads (4-5k/s) and both are at
  ~15-20% parallel efficiency against Go's 250-285k.  Migration was meant to
  fix this shape but crashes on Linux, so the c-8 finding stands: without
  migration, wake placement leaves cores idle when handlers compute.
- **Go's number is the ceiling of the shape**: ~300k iterations/s means the
  echo server and the 1 ms sleep are not the limit; the Python runtimes are
  interpreter-bound at every cpu weight.

Groups NOT run before the box was deleted: `cpu_parallel` at 10k tasks (the
64 and 1024 rows are in `results/linux32_sweep_cpu.log`; asyncio at 10k hit
the 300 s cap), mixed hub scaling 1-32, worker_pool/pipeline hub scaling,
spawn_churn at 1M.  Per-run stdout for every group is in `results/linux32_*.log`.

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
