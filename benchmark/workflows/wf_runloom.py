"""runloom implementation: stackful fibers on an M:N hub pool (GIL off).

Idioms: runloom.fiber per concurrent unit, runloom.Chan for the pool and
pipeline (Go-style, closable, iterable), runloom.sync.WaitGroup for join,
runloom.sync.tcp_connect for cooperative sockets, runloom.sleep for timers.
Plain synchronous code -- no async/await -- with real multi-core parallelism
when --workers > 1.

    PYTHON_GIL=0 PYTHONPATH=../../src python wf_runloom.py --workload X
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# src/ goes LAST so a PYTHONPATH pointing at an extension built against a
# different interpreter (build/lib-mig for runloom-mig) wins over the stock
# in-place build in src/.  Loading the stock .so into the patched interpreter
# segfaults: the alloc-home patch changes the PyThreadState layout.
sys.path.append(os.path.join(HERE, "..", "..", "src"))
import common as C  # noqa: E402
import runloom  # noqa: E402
import runloom.sync as rs  # noqa: E402


def _recv_exactly(s, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("eof")
        buf += chunk
    return bytes(buf)


def _spawn_all(n, fn, indexed=False, gate=None):
    """Run fn on n fibers.  With `gate` (a _StartGate) each fiber does its
    setup, calls gate.arrive(), and blocks until main -- having seen all n
    arrive -- marks the timed start and opens the gate: connect ramp
    outside the window, Go-style (WaitGroup + closed channel)."""
    wg = rs.WaitGroup()
    wg.add(n)

    def runner(i):
        try:
            fn(i) if indexed else fn()
        finally:
            wg.done()
    for i in range(n):
        runloom.fiber(runner, i)
    if gate is not None:
        gate.ready.wait()
        C.mark_start()
        gate.start.close()          # recv on a closed chan returns at once
    wg.wait()


class _StartGate:
    def __init__(self, n):
        self.ready = rs.WaitGroup()
        self.ready.add(n)
        self.start = runloom.Chan(0)

    def arrive(self):
        self.ready.done()
        self.start.recv()


def fanout_io(n, host, port):
    gate = _StartGate(n)

    def client(i):
        s = rs.tcp_connect(host, C.port_for(port, i))
        gate.arrive()
        try:
            for _ in range(C.FANOUT_K):
                s.sendall(C.REQ)
                _recv_exactly(s, C.REQ_LEN)
        finally:
            C.rst_close(s)
    _spawn_all(n, client, indexed=True, gate=gate)
    return n * C.FANOUT_K


def worker_pool(n):
    per = n // C.PRODUCERS
    jobs = runloom.Chan(C.QUEUE_CAP)
    results = runloom.Chan(C.QUEUE_CAP)
    total = per * C.PRODUCERS

    def producer():
        for i in range(per):
            jobs.send(i)

    def worker():
        for _ in jobs:               # ends when jobs is closed and drained
            results.send(C.hash_job())

    def collector():
        for _ in range(total):
            results.recv()

    wg_w = rs.WaitGroup()
    wg_w.add(C.WORKERS)

    def worker_runner():
        try:
            worker()
        finally:
            wg_w.done()
    for _ in range(C.WORKERS):
        runloom.fiber(worker_runner)
    wg_c = rs.WaitGroup()
    wg_c.add(1)

    def collector_runner():
        try:
            collector()
        finally:
            wg_c.done()
    runloom.fiber(collector_runner)
    _spawn_all(C.PRODUCERS, producer)
    jobs.close()
    wg_w.wait()
    wg_c.wait()
    return total


def pipeline(n):
    stages = 4
    chans = [runloom.Chan(C.PIPELINE_CAP) for _ in range(stages + 1)]

    def source():
        for i in range(n):
            chans[0].send(C.pipeline_seed(i))
        chans[0].close()

    def stage(i):
        def run():
            for item in chans[i]:
                chans[i + 1].send(C.pipeline_stage(i + 1, item))
            chans[i + 1].close()
        return run

    out = [0]
    wg = rs.WaitGroup()
    wg.add(1)

    def sink():
        try:
            cnt = 0
            for _ in chans[stages]:
                cnt += 1
            out[0] = cnt
        finally:
            wg.done()

    runloom.fiber(source)
    for i in range(stages):
        runloom.fiber(stage(i))
    runloom.fiber(sink)
    wg.wait()
    return out[0]


def cpu_parallel(n):
    _spawn_all(n, lambda: C.cpu_chain())
    return n * C.CPU_ITER


def sleepers(n):
    def task():
        for _ in range(C.SLEEP_K):
            runloom.sleep(C.SLEEP_S)
    _spawn_all(n, task)
    return n * C.SLEEP_K


def spawn_churn(n):
    _spawn_all(n, lambda: C.lcg(1, C.CHURN_ITER))
    return n


def mixed(n, host, port):
    gate = _StartGate(n)

    def handler(i):
        s = rs.tcp_connect(host, C.port_for(port, i))
        gate.arrive()
        try:
            for _ in range(C.MIXED_K):
                s.sendall(C.REQ)
                _recv_exactly(s, C.REQ_LEN)
                C.mixed_transform(C.MIXED_DOC)
                runloom.sleep(C.MIXED_DB_S)
                s.sendall(C.REQ)
                _recv_exactly(s, C.REQ_LEN)
        finally:
            C.rst_close(s)
    _spawn_all(n, handler, indexed=True, gate=gate)
    return n * C.MIXED_K


def main():
    a = C.cli()
    host, port = C.parse_addr(a.addr)
    wl = a.workload
    fn = {
        "fanout_io": lambda: fanout_io(a.n, host, port),
        "worker_pool": lambda: worker_pool(a.n),
        "pipeline": lambda: pipeline(a.n),
        "cpu_parallel": lambda: cpu_parallel(a.n),
        "sleepers": lambda: sleepers(a.n),
        "mixed": lambda: mixed(a.n, host, port),
        "spawn_churn": lambda: spawn_churn(a.n),
    }[wl]
    box = {}

    def root():
        box["ops"] = fn()

    with C.Timer() as t:
        runloom.run(a.workers, root)
    mig = runloom.migration_status()
    C.emit(os.environ.get("WF_RUNTIME_LABEL", "runloom"), wl, a.n, box["ops"],
           t.seconds, workers=a.workers, hubs=a.workers,
           backend=runloom.backend(), netpoll=runloom.netpoll_backend(),
           migration_available=mig["available"],
           migration_enabled=runloom.migration_enabled(),
           migration_active=mig["available"] and runloom.migration_enabled(),
           python_exe=sys.executable, runloom_c=__import__("runloom_c").__file__)


if __name__ == "__main__":
    main()
