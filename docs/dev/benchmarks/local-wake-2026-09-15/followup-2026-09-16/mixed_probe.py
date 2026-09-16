"""Run one workflow workload under migration with a cover build and print
the wake-routing counters next to the throughput.

  PYTHONPATH=<cover build>:<harness>/benchmark/workflows:src PYTHON_GIL=0 RUNLOOM_MIGRATION=1 \
      python mixed_probe.py --workload mixed --workers 2 --addr 127.0.0.1:19890 --n 200
"""
import os
import sys

import runloom
import runloom_c as rc

import common as C
import wf_runloom as W


def main():
    a = C.cli()
    host, port = C.parse_addr(a.addr)
    fn = {
        "mixed": lambda: W.mixed(a.n, host, port),
        "fanout_io": lambda: W.fanout_io(a.n, host, port),
        "sleepers": lambda: W.sleepers(a.n),
        "worker_pool": lambda: W.worker_pool(a.n),
    }[a.workload]
    box = {}

    def root():
        box["ops"] = fn()

    if rc._cover_enabled():
        rc._cover_reset()
    with C.Timer() as t:
        runloom.run(a.workers, root)
    mig = runloom.migration_status()
    rate = box["ops"] / t.seconds
    print("build=%s workload=%s hubs=%d n=%d mig=%s ops/s=%.0f"
          % (os.path.basename(os.path.dirname(rc.__file__)), a.workload, a.workers,
             a.n, mig["available"] and runloom.migration_enabled(), rate))
    if rc._cover_enabled():
        rep = rc._cover_report()
        keys = ("local_wake", "global_runq_pull", "steal_hit", "steal_batch", "deque_full_fallback")
        print("  cover:", {k: rep.get(k, 0) for k in keys})
    st = rc.stats()
    interesting = {k: v for k, v in st.items()
                   if any(s in k for s in ("park", "idle", "steal", "pump", "wake",
                                            "kick", "runq", "deque", "pending", "hub"))}
    print("  stats:", interesting)


if __name__ == "__main__":
    main()
