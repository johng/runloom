"""Root cause #2: the deadlock detector never fires under RUNLOOM_MIGRATION=1.

Per-g mode stands preemption down, preemption is what forces sysmon on, and the
census treats "sysmon off" as "always wakeable".  Same shape as
tests/test_mn_deadlock_detect.py::test_recv_with_no_sender_is_detected: the
main fiber blocks on an unbuffered recv nobody will ever send to.

  unset                                -> DEADLOCK RAISED after ~0.2 s
  RUNLOOM_MIGRATION=1                  -> never returns (kill it)
  RUNLOOM_MIGRATION=1 RUNLOOM_SYSMON=1 -> still hangs in most runs (2/32 raised),
                                          although the pytest deadlock files pass
"""
import os
import time

os.environ.setdefault("RUNLOOM_DEADLOCK_MS", "40")

import runloom  # noqa: E402
import runloom_c  # noqa: E402

runloom_c.set_deadlock_mode(2)   # raise


def main():
    runloom_c.Chan(0).recv()     # unbuffered, nobody will ever send


t = time.time()
try:
    runloom.run(2, main)
    print("run returned without raising after %.1fs" % (time.time() - t))
except RuntimeError as e:
    print("DEADLOCK RAISED after %.2f s: %s" % (time.time() - t, str(e)[:80]))
