# What these results were measured against

Every `*.json` here carries `env` (platform, interpreter version string, Go
version, hub count, samples) and `params` (every size constant from
`common.py`).  This file records what the JSON cannot: the exact runloom
source, how the interpreters and the extension were built, and the machines.
Re-run the same driver invocations on a later commit and diff against these
files to see what changed.

## runloom source (identical for every results file here)

| | |
| --- | --- |
| runloom commit the extension was built from | `3e9fd70aaab9d7a15d2db10ad9214f992ee72bcf` (`exec-home: make every core thread-state read a volatile inline TLS read`, 2026-09-13, branch `worktree-exec-home-cost`, PR #10) |
| relation to `main` | `origin/main` = `8a5f9de8` plus that one commit; the difference from main is 3 files (the exec-home patch and its README) |
| `git describe` | `patched-cpython-49840c658498-1-g3e9fd70a` |
| tree hash `HEAD:src` | `1e2e39fa2653525940971b290f2c8d8cb32988bc` |
| tree hash `HEAD:src/runloom_c` | `99c5c564cedac72535b762ce68de28447a32adfa` |
| tree hash `HEAD:src/runloom` | `808d66947ab0cdc27946833be3af7b84954a2cac` |
| tree hash `HEAD:src/patches` | `7f766a53bd921f19bd53589ba9cf40a19deac702` |
| blob `src/patches/cpython314t-tstate-alloc-home.patch` | `031795f4daa36aaa9fa8ed268baab1a7ab10f406` |
| blob `src/patches/cpython314t-tstate-exec-home.patch` | `9968a4a7165caa8915b9bbcbc25a098be9058a30` (lacks the thin-LTO `__attribute__((used))` fix; see below) |
| blob `setup.py` | `fb3559fd5a2704a13aa4b2476eeabace331fe4a1` |
| bench harness commit | `75dd06e9` on `bench/workflow-compare` (later commits on that branch only add results and README text unless noted in the log) |

The harness records `git_sha`, `src_tree` and `git_describe` in `env` from the
containing checkout; a rsynced tree without `.git` needs `WF_GIT_SHA`,
`WF_SRC_TREE`, `WF_GIT_DESCRIBE` in the environment.  The 2026-09-14/15 Linux
files were produced before that field existed or from a rsynced tree, so their
`git_sha` is empty: they are all commit `3e9fd70a` above.

To check a later checkout against this one:

```sh
git rev-parse HEAD:src HEAD:src/runloom_c        # compare with the tree hashes above
git diff 3e9fd70a -- src/                          # what changed in the runtime
git diff 75dd06e9 -- benchmark/workflows/*.py benchmark/workflows/wf_go   # harness drift
```

## Runtime configuration under test

- Free-threaded build, `PYTHON_GIL=0`; the specializing interpreter (TLBC)
  left on; `RUNLOOM_OFFLOAD_HUBS` unset (0); no monkey-patching.
- `runloom` = stock interpreter, `RUNLOOM_MIGRATION` unset: a parked fiber
  resumes on its home hub.  Hubs = `--workers` = `os.cpu_count()` unless a
  `@W` row says otherwise (18 on the Mac, 8 on c-8, 32 on c-32).
- `runloom-mig` = interpreter built with both `src/patches` 3.14t patches,
  extension compiled with `-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME`,
  `RUNLOOM_MIGRATION=1`; the driver refuses the label unless the process
  reports `migration_active`.
- `threads` = one OS thread per unit, 256 KiB stacks, stdlib `queue.Queue`;
  `threads-sq` = same with `queue.SimpleQueue`.  `asyncio` = one thread.
  `go` = `GOMAXPROCS` = hubs.
- Fiber stacks, channel capacities and every workload size: `params` in each
  JSON (`QUEUE_CAP=256`, `PIPELINE_CAP=128`, `CPU_ITER=400000`, `JOB_ITER=200`,
  `SLEEP_S=0.005`, `SLEEP_K=20`, `MIXED_K=20`, `MIXED_DB_S=0.001`, `CHURN_ITER=50`
  unless a row label overrides `cpu=`/`job=`).

## Machines and toolchains

| tag | files | machine | interpreter builds | extension | Go |
| --- | --- | --- | --- | --- | --- |
| macOS | `workflows.json`, `sweep_*.json`, `hubs_*.json`, `limit_*.json`, `handoff_*.json` (2026-09-14) | Apple M5 Max, 18 cores, 128 GB, Darwin 25.3.0 | 3.14.4t and 3.14.4t-mig, Apple clang 21, `--disable-gil --enable-optimizations --with-lto=thin --with-tail-call-interp`, built 2026-09-14 21:33 (`~/.pyenv/versions/`) | stock: `setup.py build_ext --inplace` (`src/runloom_c.cpython-314t-darwin.so`, 21:35); mig: same + defines via `CFLAGS` into `build/lib-mig` (21:56) | go1.26.6 darwin/arm64 |
| c-8 | `linux_*.json` (2026-09-14) | DigitalOcean c-8, Xeon Platinum 8358, 8 vCPU (4 cores SMT), 16 GB, Ubuntu 24.04, kernel 6.8, glibc 2.39 | same configure line, clang 19.1.7 (apt.llvm.org); mig patch needed `__attribute__((used))` on `_Py_tss_tstate` added by hand | mig extension built with `CFLAGS=` in the environment: replaces setuptools' base flags, so NO `-DNDEBUG` (asserts on) -- read that column as a lower bound | go1.27.1 linux/amd64 |
| c-32 | `linux32_{workflows,sweep_sleepers,sweep_fanout,sweep_mixed}.json` + `*.log` (2026-09-15; cpu sweep partial in its log, hub-scaling and 1M spawn groups not run) | DigitalOcean c-32 (QEMU guest), Xeon Platinum 8280 @ 2.70GHz, 32 vCPU (32 cores, 1 thread/core), 64 GB, Ubuntu 24.04, kernel 6.8.0-124, glibc 2.39 | same configure line, clang 19.1.1 (Ubuntu apt), lld; PGO profile task `-m test --pgo --timeout=1200 -x test_generators` (the stock task failed on `test_generators::test_raise_and_yield_from` in both trees); built 2026-09-15 19:09; mig from the exec-home patch as of `worktree-exec-home-cost` working tree (adds the `used` attribute), zero fuzz; an A/B interpreter from the pre-3e9fd70a patch behaved identically | both extensions with identical flags; mig defines via `RUNLOOM_EXTRA_CFLAGS` | go1.27.1 linux/amd64 |

Kernel/limits on the Linux boxes: `ulimit -n 1048576`,
`net.ipv4.ip_local_port_range=1024 65535`, `net.core.somaxconn=8192`,
`net.ipv4.tcp_tw_reuse=1`, `vm.max_map_count` raised.  macOS: defaults
(`kern.ipc.somaxconn=128`, ~16k ephemeral ports per local IP).

## Known defects in this snapshot (what a later comparison should look for)

1. `runloom-mig` segfaults in `run()` teardown on `mixed` and `fanout_io` on
   Linux x86-64 (any hub count, either patch revision, asserts on or off);
   `sleepers` under mig is 9x slower than stock with 165 KB/fiber.  Those cells
   are `exit -11` or suspect in `linux32_*.json`.  Write-up:
   `docs/dev/mig-mixed-teardown-crash.md` (branch `docs/mig-mixed-teardown-crash`).
2. Spawn churn: 5-125k spawns/s falling with hubs and N, 14-36 KB per pending
   fiber (`hubs_spawn.json`, `limit_spawn*.json`, `linux32_limit_spawn.json`).
3. Chan hand-off about half the speed of `queue.SimpleQueue`; worker pool
   collector serializes above 8 hubs (`handoff_*.json`, `hubs_worker_pool.json`).
4. Non-migrating wake placement idles cores on CPU-heavy `mixed`
   (`sweep_mixed.json`, `linux_sweep_mixed.json`).
5. Per-fiber memory 18-53 KB vs 2-17 KB for asyncio (`sweep_sleepers.json`,
   `limit_sleepers.json`).
