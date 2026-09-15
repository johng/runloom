#!/usr/bin/env python3
"""Drive the workflow comparison: asyncio vs free-threaded threads vs runloom vs Go.

Starts ONE Go echo server (the fixed backend for the network workloads),
then runs every (runtime, workload) pair as its own subprocess, `--samples`
times after `--warmup` discarded runs, and reports the median ops/s per
cell plus each runtime's ratio to Go.  Results are written to
results/workflows.json with the environment recorded.

    PYTHON_GIL=0 ~/.pyenv/versions/3.14.4t/bin/python3.14 run_workflows.py
    ... --workloads fanout_io,mixed --runtimes runloom,go --n 1000

Requires: `go` on PATH, the runloom C extension built in ../../src, and a
free-threaded Python (the threads/runloom columns refuse to run GIL-on).
"""
import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common as C  # noqa: E402

RUNTIMES = ("asyncio", "threads", "threads-sq", "runloom", "runloom-mig", "go")
# threads-sq: the threads implementation with queue.SimpleQueue (C, unbounded)
# in place of queue.Queue (pure Python, bounded) -- the fastest stdlib hand-off.
# runloom-mig: runloom with cross-hub fiber migration ON.  Needs a CPython
# built with BOTH src/patches (alloc-home + exec-home) and the extension
# compiled against it with the same defines (they are compile-time):
#   RUNLOOM_EXTRA_CFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME" \
#       <mig-python> setup.py build_ext -f --build-lib build/lib-mig --build-temp build/temp-mig
# (RUNLOOM_EXTRA_CFLAGS is APPENDED; a plain CFLAGS= in the environment
# replaces the interpreter's base flags in setuptools and drops -DNDEBUG.)
# Plain `runloom` = stock interpreter, migration off (a parked fiber always
# resumes on the hub it parked on).


def _git(*args, env):
    """git output for the checkout containing this file, or the env override."""
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)
    out = r.stdout.strip() if r.returncode == 0 else ""
    return out or os.environ.get(env, "")


GO_DIR = os.path.join(HERE, "wf_go")
GO_BIN = os.path.join(GO_DIR, "wf_go")
RESULTS = os.path.join(HERE, "results")


def build_go():
    subprocess.check_call(["go", "build", "-o", GO_BIN, "."], cwd=GO_DIR)


def start_echo(addr, env):
    p = subprocess.Popen([GO_BIN, "-echoserver", "-addr", addr],
                         stdout=subprocess.PIPE, text=True, env=env)
    line = p.stdout.readline()
    if not line.startswith("LISTENING"):
        p.kill()
        raise SystemExit("echo server failed to start: %r" % line)
    return p


def command(runtime, workload, n, workers, addr, python):
    if runtime == "go":
        return [GO_BIN, "-workload", workload, "-n", str(n), "-addr", addr,
                "-procs", str(workers)]
    script = os.path.join(HERE, "wf_%s.py" % runtime.split("-")[0])
    return [python, script, "--workload", workload, "--n", str(n),
            "--workers", str(workers), "--addr", addr]


def run_once(cmd, env, timeout):
    t0 = time.perf_counter()
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       timeout=timeout)
    wall = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError("exit %d: %s" % (r.returncode, r.stderr.strip()[-2000:]))
    doc = json.loads(r.stdout.strip().splitlines()[-1])
    doc["process_wall_s"] = wall
    if doc.get("runtime") == "runloom-mig" and not doc.get("migration_active"):
        raise RuntimeError("runloom-mig ran WITHOUT migration (available=%s enabled=%s); "
                           "extension not built against the patched interpreter?"
                           % (doc.get("migration_available"), doc.get("migration_enabled")))
    return doc


def median_cell(runs):
    rates = [r["ops_per_s"] for r in runs]
    med = statistics.median(rates)
    mad = statistics.median([abs(x - med) for x in rates]) if len(rates) > 1 else 0.0
    med_of = lambda k: statistics.median([r.get(k, 0) for r in runs])
    return {"ops_per_s": med, "mad": mad, "rsd_pct": (100.0 * mad / med) if med else 0.0,
            "seconds": statistics.median([r["seconds"] for r in runs]),
            "ops": runs[0]["ops"], "n": runs[0]["n"],
            "workers": runs[0].get("workers"), "samples": len(runs),
            "rss_start_bytes": med_of("rss_start_bytes"),
            "rss_peak_bytes": med_of("rss_peak_bytes"),
            "rss_delta_bytes": med_of("rss_delta_bytes"),
            "bytes_per_unit": med_of("bytes_per_unit"),
            "raw_ops_per_s": rates}


def fmt_bytes(x):
    if x >= 1 << 30:
        return "%.2fG" % (x / (1 << 30))
    if x >= 1 << 20:
        return "%.1fM" % (x / (1 << 20))
    if x >= 1 << 10:
        return "%.0fk" % (x / (1 << 10))
    return "%.0f" % x


def print_mem_table(table, runtimes, workloads):
    """Peak-RSS delta of the workload (memory the N units cost at their
    high-water mark) and that delta per unit."""
    w = 13
    wu = 17
    print("%-26s" % "rss delta" + "".join("%*s" % (w, r) for r in runtimes)
          + "   " + "".join("%*s" % (wu, r + "/unit") for r in runtimes))
    for wl in workloads:
        row = "%-26s" % wl
        for r in runtimes:
            cell = table.get(wl, {}).get(r)
            row += "%*s" % (w, fmt_bytes(cell["rss_delta_bytes"])
                            if cell and "rss_delta_bytes" in cell else "-")
        row += "   "
        for r in runtimes:
            cell = table.get(wl, {}).get(r)
            row += "%*s" % (wu, fmt_bytes(cell["bytes_per_unit"])
                            if cell and "bytes_per_unit" in cell else "-")
        print(row)
    print()


def fmt_rate(x):
    if x >= 1e6:
        return "%.2fM" % (x / 1e6)
    if x >= 1e3:
        return "%.1fk" % (x / 1e3)
    return "%.0f" % x


def print_table(table, runtimes, workloads):
    w = 13
    wu = 14
    print()
    print("%-26s" % "ops/s" + "".join("%*s" % (w, r) for r in runtimes)
          + "   " + "".join("%*s" % (w + 3, r + "/go") for r in runtimes if r != "go"))
    for wl in workloads:
        row = "%-26s" % wl
        go = table.get(wl, {}).get("go")
        for r in runtimes:
            cell = table.get(wl, {}).get(r)
            row += "%*s" % (w, fmt_rate(cell["ops_per_s"]) if cell and "ops_per_s" in cell
                            else (cell or {}).get("error", "-")[:w - 1])
        row += "   "
        for r in runtimes:
            if r == "go":
                continue
            cell = table.get(wl, {}).get(r)
            if go and cell and "ops_per_s" in cell and go.get("ops_per_s"):
                row += "%*s" % (w + 3, "%.2fx" % (cell["ops_per_s"] / go["ops_per_s"]))
            else:
                row += "%*s" % (w + 3, "-")
        print(row)
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workloads", default=",".join(C.WORKLOADS))
    ap.add_argument("--runtimes", default=",".join(RUNTIMES))
    ap.add_argument("--n", type=int, default=0, help="override every workload's N")
    ap.add_argument("--ns", default="", help="comma list of N values: sweep each workload over them")
    ap.add_argument("--workers-list", default="",
                    help="comma list of worker counts (runloom hubs / GOMAXPROCS) to sweep. "
                         "asyncio/threads have no such knob: they run ONCE and that cell is "
                         "repeated on every @w row as the fixed Python baseline")
    ap.add_argument("--mixed-cpu", default="", help="comma list of WF_MIXED_CPU values for the mixed workload")
    ap.add_argument("--job-iter", default="", help="comma list of WF_JOB_ITER values for worker_pool (0 = pure hand-off)")
    ap.add_argument("--ports", type=int, default=1,
                    help="echo-server ports (spread clients; loopback has ~16k ephemeral ports per port)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                    help="runloom hubs / GOMAXPROCS (threads and asyncio ignore it)")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--addr", default="127.0.0.1:19876")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--mig-python",
                    default=os.path.expanduser("~/.pyenv/versions/3.14.4t-mig/bin/python3.14"),
                    help="patched (alloc-home + exec-home) interpreter for runloom-mig")
    ap.add_argument("--mig-lib", default=os.path.join(HERE, "..", "..", "build", "lib-mig"),
                    help="dir holding runloom_c built against --mig-python")
    ap.add_argument("--out", default=os.path.join(RESULTS, "workflows.json"))
    ap.add_argument("--baselines", default="threads,asyncio",
                    help="ratio columns are each runtime as a multiple of these (comma list)")
    ap.add_argument("--no-scaling", action="store_true",
                    help="skip the extra cpu_parallel run at workers=1 for runloom/go")
    a = ap.parse_args()
    workloads = [w for w in a.workloads.split(",") if w]
    runtimes = [r for r in a.runtimes.split(",") if r]
    for w in workloads:
        if w not in C.WORKLOADS:
            raise SystemExit("unknown workload %s" % w)
    for r in runtimes:
        if r not in RUNTIMES:
            raise SystemExit("unknown runtime %s" % r)
    if not shutil.which("go"):
        raise SystemExit("go not on PATH")
    if "runloom-mig" in runtimes:
        if not os.path.exists(a.mig_python):
            raise SystemExit("runloom-mig: no interpreter at %s (--mig-python)" % a.mig_python)
        if not (os.path.isdir(a.mig_lib)
                and any(f.startswith("runloom_c") for f in os.listdir(a.mig_lib))):
            raise SystemExit("runloom-mig: no runloom_c in %s (--mig-lib); see RUNTIMES note" % a.mig_lib)
    build_go()

    env = dict(os.environ)
    env["PYTHON_GIL"] = "0"
    env["PYTHONPATH"] = os.path.join(HERE, "..", "..", "src")
    env["WF_PORTS"] = str(a.ports)
    # Big fan-outs need fds: raise the soft limit to the hard one for us and
    # every child (echo server included).
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = hard if hard != resource.RLIM_INFINITY else 1 << 20
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ImportError, ValueError, OSError):
        pass

    # Cells to run: every (workload, runtime), plus -- because core scaling is
    # the whole point of M:N -- cpu_parallel again at ONE worker for the
    # runtimes that have the knob, so the table shows speedup, not just rate.
    ns = [int(x) for x in a.ns.split(",") if x] or [a.n]
    cpus = [int(x) for x in a.mixed_cpu.split(",") if x] or [None]
    jobs = [int(x) for x in a.job_iter.split(",") if x] or [None]
    wlist = [int(x) for x in a.workers_list.split(",") if x] or [a.workers]
    NO_KNOB = ("asyncio", "threads", "threads-sq")   # one OS thread / N OS threads regardless
    # cell = (workload, runtime, workers, n, mixed_cpu)
    cells = []
    for wl in workloads:
        for n in ns:
            for cpu in (cpus if wl == "mixed" else (jobs if wl == "worker_pool" else [None])):
                for w in wlist:
                    for rt in runtimes:
                        if rt in NO_KNOB and w != wlist[0]:
                            continue        # filled in from the first row below
                        cells.append((wl, rt, w, n, cpu))
    if ("cpu_parallel" in workloads and not a.no_scaling and a.workers > 1
            and not a.workers_list):
        cells += [("cpu_parallel", rt, 1, n, None) for n in ns
                  for rt in runtimes if rt in ("runloom", "runloom-mig", "go")]

    echo = start_echo(a.addr, env)
    table = {}
    try:
        for wl, rt, workers, n, cpu in cells:
            n = n or C.DEFAULT_N[wl]
            row = wl
            if len(ns) > 1:
                row += " n=%d" % n
            if cpu is not None:
                row += (" cpu=%d" if wl == "mixed" else " job=%d") % cpu
            if workers != a.workers or a.workers_list:
                row += "@%d" % workers
            table.setdefault(row, {})
            py = a.mig_python if rt == "runloom-mig" else a.python
            cmd = command(rt, wl, n, workers, a.addr, py)
            cenv = dict(env)
            if cpu is not None:
                cenv["WF_MIXED_CPU" if wl == "mixed" else "WF_JOB_ITER"] = str(cpu)
            if rt == "threads-sq":
                cenv["WF_THREADS_QUEUE"] = "simple"
                cenv["WF_RUNTIME_LABEL"] = "threads-sq"
            if rt == "runloom-mig":
                cenv["RUNLOOM_MIGRATION"] = "1"
                cenv["WF_RUNTIME_LABEL"] = "runloom-mig"
                cenv["PYTHONPATH"] = os.path.abspath(a.mig_lib) + os.pathsep + env["PYTHONPATH"]
            runs = []
            try:
                for _ in range(a.warmup):
                    run_once(cmd, cenv, a.timeout)
                for _ in range(a.samples):
                    runs.append(run_once(cmd, cenv, a.timeout))
                cell = median_cell(runs)
                print("  %-24s %-8s n=%-7d %10s ops/s  (%.3fs, rsd %.1f%%)  rss +%s (%s/unit)"
                      % (row, rt, n, fmt_rate(cell["ops_per_s"]),
                         cell["seconds"], cell["rsd_pct"],
                         fmt_bytes(cell["rss_delta_bytes"]), fmt_bytes(cell["bytes_per_unit"])))
            except Exception as e:  # keep going; record the failure
                cell = {"error": str(e).splitlines()[0][:200]}
                print("  %-24s %-8s n=%-7d FAILED: %s" % (row, rt, n, cell["error"]))
            table[row][rt] = cell
            sys.stdout.flush()
    finally:
        echo.kill()
        echo.wait()

    # Hub sweep: copy the knob-less runtimes' single measurement onto every
    # @w row so each row is a complete comparison.  Marked as a copy in JSON.
    if a.workers_list:
        for row in list(table):
            if "@%d" % wlist[0] not in row:
                continue
            base = row.rsplit("@", 1)[0]
            for w in wlist[1:]:
                other = "%s@%d" % (base, w)
                if other not in table:
                    continue
                for rt in NO_KNOB:
                    if rt in table[row] and rt not in table[other]:
                        table[other][rt] = dict(table[row][rt], copied_from=row)

    import report
    bl = [b for b in a.baselines.split(",") if b in runtimes]
    rows = sorted(table, key=report.row_key)
    print()
    report.block(table, rows, runtimes, bl, "ops_per_s", report.fmt_rate, "ops/s")
    report.block(table, rows, runtimes, bl, "rss_delta_bytes", report.fmt_bytes, "rss delta")
    report.block(table, rows, runtimes, bl, "bytes_per_unit", report.fmt_bytes, "bytes/unit")
    doc = {
        "suite": "workflows",
        "when": datetime.now(timezone.utc).isoformat(),
        "env": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "nproc": os.cpu_count(),
            "python": a.python,
            "python_version": subprocess.run([a.python, "-c", "import sys;print(sys.version)"],
                                             capture_output=True, text=True).stdout.strip(),
            "go": subprocess.run(["go", "version"], capture_output=True, text=True).stdout.strip(),
            # Full runloom commit and the tree hash of src/ (what the extension
            # was built from), so two results files can be compared at a later
            # date.  A rsynced checkout has no .git: set WF_GIT_SHA / WF_SRC_TREE.
            "git_sha": _git("rev-parse", "HEAD", env="WF_GIT_SHA"),
            "src_tree": _git("rev-parse", "HEAD:src", env="WF_SRC_TREE"),
            "git_describe": _git("describe", "--tags", "--always", "--dirty", env="WF_GIT_DESCRIBE"),
            "workers": a.workers, "samples": a.samples, "warmup": a.warmup,
        },
        "params": {k: getattr(C, k) for k in dir(C) if k.isupper() and not k.startswith("_")
                   and isinstance(getattr(C, k), (int, float, str, dict, tuple))},
        "results": table,
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True, default=str)
        f.write("\n")
    print("wrote", a.out)


if __name__ == "__main__":
    main()
