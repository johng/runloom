"""Free-threaded CPython implementation: one OS thread per task, the GIL off.

This is the "just use threads" baseline that free-threading makes viable:
blocking sockets, queue.Queue for the pool and pipeline, time.sleep for
timers, threading.Thread per concurrent unit.  No scheduler at all -- the
kernel is the scheduler, and the cost of that (thread creation, kernel
context switches, per-thread stacks) is exactly what shows up at high N.

Refuses to run with the GIL on: a GIL'd number in a column labelled
free-threading is worse than no number.
"""
import os
import queue
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def _connect(host, port):
    s = socket.create_connection((host, port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def _recv_exactly(s, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("eof")
        buf += chunk
    return bytes(buf)


def _spawn_all(n, fn, indexed=False, barrier=None):
    """Run fn on n threads.  With `barrier` (a Barrier of n+1 parties whose
    action is C.mark_start) each thread does its setup and waits on it; the
    LAST arrival runs the action, so the timed start is marked BEFORE any
    thread is released.  (Marking from main after its own wait() returns is
    wrong: with thousands of runnable threads main can be descheduled for
    hundreds of ms first, shrinking the window and inflating ops/s.)"""
    ts = [threading.Thread(target=fn, args=(i,) if indexed else ())
          for i in range(n)]
    for t in ts:
        t.start()
    if barrier is not None:
        barrier.wait()
    for t in ts:
        t.join()


def fanout_io(n, host, port):
    bar = threading.Barrier(n + 1, action=C.mark_start)

    def client(i):
        s = _connect(host, C.port_for(port, i))
        bar.wait()
        try:
            for _ in range(C.FANOUT_K):
                s.sendall(C.REQ)
                _recv_exactly(s, C.REQ_LEN)
        finally:
            C.rst_close(s)
    _spawn_all(n, client, indexed=True, barrier=bar)
    return n * C.FANOUT_K


# WF_THREADS_QUEUE=simple swaps the bounded pure-Python queue.Queue (mutex +
# condition variables) for the C-implemented, UNBOUNDED queue.SimpleQueue --
# the fastest stdlib hand-off, at the cost of no back-pressure.
def _make_queue(cap):
    if os.environ.get("WF_THREADS_QUEUE") == "simple":
        return queue.SimpleQueue()
    return queue.Queue(cap)


def worker_pool(n):
    per = n // C.PRODUCERS
    jobs = _make_queue(C.QUEUE_CAP)
    results = _make_queue(C.QUEUE_CAP)
    total = per * C.PRODUCERS

    def producer():
        for i in range(per):
            jobs.put(i)

    def worker():
        while True:
            j = jobs.get()
            if j is None:
                return
            results.put(C.hash_job())

    def collector():
        for _ in range(total):
            results.get()

    coll = threading.Thread(target=collector)
    coll.start()
    workers = [threading.Thread(target=worker) for _ in range(C.WORKERS)]
    for w in workers:
        w.start()
    _spawn_all(C.PRODUCERS, producer)
    for _ in range(C.WORKERS):
        jobs.put(None)
    for w in workers:
        w.join()
    coll.join()
    return total


def pipeline(n):
    stages = 4
    qs = [_make_queue(C.PIPELINE_CAP) for _ in range(stages + 1)]

    def source():
        for i in range(n):
            qs[0].put(C.pipeline_seed(i))
        qs[0].put(None)

    def stage(i):
        while True:
            item = qs[i].get()
            if item is None:
                qs[i + 1].put(None)
                return
            qs[i + 1].put(C.pipeline_stage(i + 1, item))

    out = [0]

    def sink():
        while True:
            item = qs[stages].get()
            if item is None:
                return
            out[0] += 1

    ts = [threading.Thread(target=source)] + \
         [threading.Thread(target=stage, args=(i,)) for i in range(stages)] + \
         [threading.Thread(target=sink)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out[0]


def cpu_parallel(n):
    _spawn_all(n, lambda: C.cpu_chain())
    return n * C.CPU_ITER


def sleepers(n):
    def task():
        for _ in range(C.SLEEP_K):
            time.sleep(C.SLEEP_S)
    _spawn_all(n, task)
    return n * C.SLEEP_K


def spawn_churn(n):
    # Start-and-join in windows so a million Thread objects never exist at
    # once (the per-process thread cap is ~16k on macOS anyway); each window
    # is still "spawn as fast as possible, join all".
    win = 4096
    for start in range(0, n, win):
        _spawn_all(min(win, n - start), lambda: C.lcg(1, C.CHURN_ITER))
    return n


def mixed(n, host, port):
    bar = threading.Barrier(n + 1, action=C.mark_start)

    def handler(i):
        s = _connect(host, C.port_for(port, i))
        bar.wait()
        try:
            for _ in range(C.MIXED_K):
                s.sendall(C.REQ)
                _recv_exactly(s, C.REQ_LEN)
                C.mixed_transform(C.MIXED_DOC)
                time.sleep(C.MIXED_DB_S)
                s.sendall(C.REQ)
                _recv_exactly(s, C.REQ_LEN)
        finally:
            C.rst_close(s)
    _spawn_all(n, handler, indexed=True, barrier=bar)
    return n * C.MIXED_K


def main():
    a = C.cli()
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    if gil:
        raise SystemExit("wf_threads: GIL is enabled; run on a free-threaded "
                         "build with PYTHON_GIL=0")
    # Thread stacks: 10k+ threads at the default 8 MiB reserve is fine
    # virtually but slow to map; 256 KiB matches what the work here needs.
    threading.stack_size(int(os.environ.get("WF_THREAD_STACK", 256 * 1024)))
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
    with C.Timer() as t:
        ops = fn()
    C.emit(os.environ.get("WF_RUNTIME_LABEL", "threads"), wl, a.n, ops, t.seconds,
           workers=os.cpu_count(), queue_impl=os.environ.get("WF_THREADS_QUEUE", "Queue"))


if __name__ == "__main__":
    main()
