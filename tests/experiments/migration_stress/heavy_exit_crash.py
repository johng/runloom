"""Root cause #1: SEGV at interpreter exit under RUNLOOM_MIGRATION=1.

The heavy-offload workload from tests/test_sysmon_oracle.py.  A RunloomG whose
last ref is dropped on a blockpool worker is deallocated from a hub tstate's
biased-refcount merge during Py_FinalizeEx; runloom_g_decref then clears the
fiber's per-g tstate a second time (CPython already tore it down) and
_Py_brc_remove_thread -> llist_remove derefs NULL.

Deterministic with sysmon on (set below, as the test does); without it the
deferred dealloc lands in the merge only some of the time (~1 run in 3).

  RUNLOOM_MIGRATION=1 -> prints OK, exits 139 (SIGSEGV).   unset -> exits 0.
"""
import hashlib
import os

os.environ.setdefault("RUNLOOM_SYSMON", "1")
os.environ.setdefault("RUNLOOM_SYSMON_MS", "20")

import runloom
import runloom.monkey
import runloom_c

runloom.monkey.patch(heavy=True)
BUF = b"x" * (8 * 1024 * 1024)


def g():
    for _ in range(25):
        hashlib.sha256(BUF).digest()


runloom_c.mn_init(4)
for _ in range(4):
    runloom_c.mn_fiber(g)
runloom_c.mn_run()
print("OK", flush=True)
