"""Shared definitions for the workflow comparison benchmarks.

Every runtime implementation (asyncio, free-threaded threads, runloom, Go)
performs the SAME workload with the SAME parameters and prints ONE JSON line:

    {"runtime": "...", "workload": "...", "n": N, "ops": total_ops,
     "seconds": wall, "ops_per_s": rate, "workers": W,
     "rss_start_bytes": ..., "rss_peak_bytes": ..., "rss_delta_bytes": ...,
     "bytes_per_unit": rss_delta / N}

Memory: every implementation records its resident set size before the
workload and its peak afterwards; the delta is what the N concurrent
units cost at their high-water mark, and delta/N is the per-unit figure
(per client, per sleeper, per handler ...).  For worker_pool / pipeline
N is a job/item count, so bytes_per_unit there is per queued item, not
per task.

`run_workflows.py` drives each implementation as a subprocess, so the
runtimes never share a process (a C extension that flips the GIL on, an
event loop left running, or a leaked thread cannot contaminate a neighbour).

Workloads -- each is a shape that shows up in ordinary services, not a
scheduler microbenchmark:

  fanout_io    N concurrent clients, each does K sequential request/response
               round-trips over TCP to an echo server (the "call a backend
               service from N handlers" shape).  ops = N*K round-trips.
               All N connections are established BEFORE the timed window
               (a barrier), so the number is round-trip throughput, not
               the connect ramp / listen-backlog behaviour.
  worker_pool  P producers push J jobs each onto a bounded queue; W workers
               pull, run a small CPU step, push to a results queue; one collector
               drains.  ops = P*J jobs.
  pipeline     4-stage bounded-channel pipeline, each stage a small
               json/str transform.  ops = items through the last stage.
  cpu_parallel N tasks each running a pure-Python integer loop; the core-scaling
               shape (asyncio caps at one core by construction).
               ops = N*ITER hashes.
  sleepers     N concurrent tasks each sleeping S ms K times (the "wait on a
               slow remote" shape; pure timer scaling).  ops = N*K wakeups.
  mixed        N concurrent handlers, each K iterations of: TCP round-trip,
               json decode+encode, 1 ms sleep (fake DB), TCP round-trip.
               ops = N*K handler iterations.
  spawn_churn  N short-lived tasks (a few us of work each), spawned as fast
               as the runtime allows and joined (the task-per-request shape;
               measures spawn + exit + join cost).  ops = N tasks.
"""
import json
import os
import resource
import sys
import time

WORKLOADS = ("fanout_io", "worker_pool", "pipeline", "cpu_parallel",
             "sleepers", "mixed", "spawn_churn")

# Per-workload defaults (overridable via the driver's --n / env).
DEFAULT_N = {
    "fanout_io": 500,       # concurrent clients
    "worker_pool": 20000,   # total jobs (P producers * J each)
    "pipeline": 50000,      # items
    "cpu_parallel": 64,     # tasks
    "sleepers": 5000,       # concurrent sleepers
    "mixed": 200,           # concurrent handlers
    "spawn_churn": 100000,  # short-lived tasks
}

# Fixed inner sizes so ops are comparable across runtimes.
FANOUT_K = 50              # round-trips per client
PRODUCERS = 8
WORKERS = 32
QUEUE_CAP = 256
PIPELINE_CAP = 128
CPU_ITER = 400000          # LCG steps per task (~15 ms single-core Python)
# LCG steps per worker_pool job (~8 us).  Env so the Go side reads the same
# value; 0 isolates pure queue hand-off cost.
JOB_ITER = int(os.environ.get("WF_JOB_ITER", "200"))
SLEEP_S = 0.005            # 5 ms
SLEEP_K = 20               # sleeps per task
MIXED_K = 20
MIXED_DB_S = 0.001
CHURN_ITER = 50            # LCG steps per short-lived task (~2.5 us)
# CPU per mixed iteration, in LCG steps (~20 steps/us of Python).  200 is
# ~10 us (I/O-bound handler); 4000 ~200 us (typical JSON/ORM request);
# 20000 ~1 ms (heavy).  Env so the Go side reads the same value.
MIXED_CPU = int(os.environ.get("WF_MIXED_CPU", "200"))
# Loopback has ~16k ephemeral ports per (dst ip, dst port); the echo server
# listens on WF_PORTS consecutive ports and clients spread across them so
# a 50k-connection fan-out is possible without extra IPs.
PORTS = int(os.environ.get("WF_PORTS", "1"))

REQ = b"hello-workflow-\n"   # 16 bytes; echo server returns it verbatim
REQ_LEN = len(REQ)

MIXED_DOC = json.dumps({"id": 1, "user": "u" * 16, "items": list(range(10)),
                        "tags": ["a", "b", "c"]}).encode()


# CPU kernels are PURE PYTHON on purpose.  hashlib.sha256 looked like the
# obvious choice but does not scale on a free-threaded build (OpenSSL's
# EVP fetch path serializes: 8 threads ran at 0.85x of one), which would
# have made every runtime look single-core.  A 32-bit LCG step touches no
# shared C state, so with the GIL off it scales with cores.  The Go side
# runs the identical recurrence.
LCG_A = 1103515245
LCG_C = 12345
LCG_MASK = 0xFFFFFFFF


def lcg(x, n):
    for _ in range(n):
        x = (x * LCG_A + LCG_C) & LCG_MASK
    return x


def hash_job(seed=1, rounds=JOB_ITER):
    """The worker_pool CPU step: JOB_ITER LCG steps (~10 us of Python)."""
    return lcg(seed, rounds)


def cpu_chain(n=CPU_ITER):
    return lcg(1, n)


def pipeline_stage(i, item):
    """Stage i transform: decode -> mutate -> encode.  Mirrors the Go side."""
    d = json.loads(item)
    d["stage"] = i
    d["seq"] = d["seq"] + 1
    return json.dumps(d)


def pipeline_seed(i):
    return json.dumps({"seq": i, "stage": 0, "body": "b" * 64})


def mixed_transform(doc):
    d = json.loads(doc)
    d["items"] = [v * 2 for v in d["items"]]
    lcg(d["id"], MIXED_CPU)          # the request's "business logic"
    return json.dumps(d).encode()


def parse_addr(s):
    host, _, port = s.rpartition(":")
    return host or "127.0.0.1", int(port)


LINGER_RST = __import__("struct").pack("ii", 1, 0)


def rst_close(sock):
    """Close a client socket with an RST (SO_LINGER on, 0 s) so it leaves no
    TIME_WAIT behind: macOS holds ~16k ephemeral ports per local IP for
    2*MSL after a FIN close, which exhausts the port space across runs."""
    import socket as _s
    try:
        sock.setsockopt(_s.SOL_SOCKET, _s.SO_LINGER, LINGER_RST)
    except OSError:
        pass
    sock.close()


def port_for(base_port, i):
    """Port for the i-th client: spread round-robin over PORTS ports."""
    return base_port + (i % PORTS)


def peak_rss_bytes():
    """High-water resident set size of this process, in bytes.

    ru_maxrss is bytes on macOS and kilobytes on Linux/BSD.  Peak, not
    current, is what we want: the concurrent units all exist at once and
    the interesting number is how much memory that moment cost."""
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v if sys.platform == "darwin" else v * 1024


# Captured at import, i.e. before any workload allocates, so that
# peak - start is the memory the workload itself added on top of the
# interpreter + imports.  (Peak at this point == current: nothing has
# been freed yet.)
RSS_START = peak_rss_bytes()


def emit(runtime, workload, n, ops, seconds, **extra):
    peak = peak_rss_bytes()
    doc = {"runtime": runtime, "workload": workload, "n": n, "ops": ops,
           "seconds": seconds, "ops_per_s": ops / seconds if seconds else 0.0,
           "rss_start_bytes": RSS_START, "rss_peak_bytes": peak,
           "rss_delta_bytes": peak - RSS_START,
           "bytes_per_unit": (peak - RSS_START) / n if n else 0.0,
           "python": sys.version.split()[0]}
    gil = getattr(sys, "_is_gil_enabled", None)
    if gil is not None:
        doc["gil_enabled"] = gil()
    if workload == "mixed":
        doc["mixed_cpu"] = MIXED_CPU
    if workload == "worker_pool":
        doc["job_iter"] = JOB_ITER
    doc.update(extra)
    sys.stdout.write(json.dumps(doc) + "\n")
    sys.stdout.flush()


def cli(argv=None):
    """Tiny shared arg parser: --workload W --n N --workers H --addr host:port."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--workload", required=True, choices=WORKLOADS)
    p.add_argument("--n", type=int, default=0)
    p.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                   help="hubs (runloom) / GOMAXPROCS-equivalent; ignored by asyncio")
    p.add_argument("--addr", default="127.0.0.1:19876",
                   help="echo server address for fanout_io / mixed")
    a = p.parse_args(argv)
    if a.n <= 0:
        a.n = DEFAULT_N[a.workload]
    return a


class Timer:
    """Times a workload.  A workload with a setup phase that must NOT be
    measured (fanout_io / mixed establish all N connections first, as the
    repo's benching rule says: measure the runtime, not the connect ramp)
    calls mark_start() once setup is done; seconds then counts from there."""
    _started = None

    def __enter__(self):
        Timer._started = None
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *a):
        t0 = Timer._started if Timer._started is not None else self.t0
        self.seconds = time.perf_counter() - t0
        self.setup_seconds = (t0 - self.t0)


def mark_start():
    Timer._started = time.perf_counter()
