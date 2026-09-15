"""Root cause #3: fiber_n bulk-arena fibers never run under RUNLOOM_MIGRATION=1.

The RUNLOOM_GON_BULK=1 batch path allocates no per-g tstates and the hub loop
skips a g with tstate == NULL as dead, so the batch never drains.  Body lifted
from tests/test_cov100_init_fini.py::test_fiber_n_bulk_wakes_idle_hubs.

  RUNLOOM_MIGRATION=1                -> hangs in WaitGroup.wait; faulthandler dumps at 8 s
  unset, or RUNLOOM_GON_BULK unset   -> "bulk woke in 0.001s" / "seen 32 OK"
"""
import faulthandler
import os
import time

os.environ.setdefault("RUNLOOM_GON_BULK", "1")

import runloom_c as rc  # noqa: E402
from runloom.sync import WaitGroup  # noqa: E402

faulthandler.dump_traceback_later(8, exit=True)
N = 32
seen = bytearray(N)


def main():
    rc.sched_sleep(0.06)            # let the other hubs reach their idle wait
    wg = WaitGroup()
    wg.add(N)

    def w(i):
        if 0 <= i < N:
            seen[i] = 1
        wg.done()

    t0 = time.monotonic()
    rc.fiber_n(lambda i: w(i), N, 0, True)   # bulk splice across idle hubs
    wg.wait()
    print("bulk woke in %.3fs" % (time.monotonic() - t0), flush=True)


rc.mn_init(4)
rc.mn_fiber(main)
rc.mn_run()
rc.mn_fini()
print("seen", sum(seen), "OK", flush=True)
