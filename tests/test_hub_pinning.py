"""Minimal working example: a fiber spawns on hub 0, then resumes where it says.

    mn_fiber(fn, hub=0)   spawn on hub 0 and stay there (no work-stealing)
    G.pin(N)              confine the next resume to hub N

Placement is otherwise deliberately free, so migration could previously only be
measured statistically ("~80% of wakes landed elsewhere").  Every destination is
asserted here, which is what stops this passing by accident: with NO pin this
workload resumes on hub 0 every time (measured 40/40), so pin(1)/(2)/(3) each
demand an outcome the unpinned scheduler does not produce, and pin(0) is the
stayed-put control.  A pin degraded to a no-op fails three of the four.

Run directly:  PYTHON_GIL=0 PYTHONPATH=src python tests/test_hub_pinning.py
"""
import os

# Read once at the first mn_init, so it has to be set before the runtime starts.
os.environ.setdefault("RUNLOOM_MIGRATION", "1")

import pytest

import runloom
import runloom_c as rc

from adv_util import needs_free_threading

HUBS = 4


def spawn_on_0_resume_on(hub):
    handoff, res = rc.Chan(1), rc.Chan(2)

    def fiber():
        g = rc.current_g()
        g.pin(hub)                        # next resume must be on this hub
        handoff.send(g)
        res.send(rc.mn_current_hub())     # spawned on hub 0
        rc.park()
        res.send(rc.mn_current_hub())     # resumed on `hub`

    def waker():
        g, _ = handoff.recv()
        # Wait until it has genuinely suspended.  NOT a correctness guard --
        # park_safe/wake_safe already absorbs an early wake, and that is the
        # problem: a fiber that never suspends never migrates, so without this
        # the moved-hub cases read (0, 0) about 15% of the time.
        while g.stack()["state"] != "parked":
            rc.yield_()
        g.wake()                          # ordinary wake; the pin picked the hub

    rc.mn_init(HUBS)
    rc.mn_fiber(fiber, hub=0)
    rc.mn_fiber(waker, hub=1)
    rc.mn_run()
    before, _ = res.try_recv()
    after, _ = res.try_recv()
    rc.mn_fini()
    return before, after


@pytest.mark.skipif(not needs_free_threading(), reason="needs a free-threaded build")
@pytest.mark.skipif(not runloom.migration_available(),
                    reason="needs both CPython migration patches (src/patches/)")
@pytest.mark.parametrize("hub", range(HUBS))   # 0 stays put; 1-3 must move
def test_fiber_resumes_on_the_hub_it_pinned_itself_to(hub):
    assert spawn_on_0_resume_on(hub) == (0, hub)


if __name__ == "__main__":
    if runloom.migration_available():
        for hub in range(HUBS):
            print("pin(%d) -> ran on hub %d, then hub %d"
                  % ((hub,) + spawn_on_0_resume_on(hub)))
    else:
        print("skipped --", runloom.migration_status())
