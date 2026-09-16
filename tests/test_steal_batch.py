"""Go-style batch steal (runqgrab): a thief takes up to half of a victim's deque.

The steal site in hub_main used to take ONE g per pick step.  That cannot
spread a BATCH -- a bulk spawn piling up on the spawner's hub, or a netpoll
pump waking N parked fibers onto the pumping hub's deque under local wake --
because the owner pops the batch faster than a single-item thief takes it,
so with two hubs the batch runs serially on the owner.  Now the thief keeps
the first g to run and moves up to half of the rest onto its OWN deque
(owner push), as repeated CAS-validated single-item steals; the deque
protocol itself is unchanged.

These tests pin down: every g in a batch still runs exactly once, the run
reaches quiescence, and, on a -DRUNLOOM_COVER build, that extras were in
fact taken (`steal_batch`) and that RUNLOOM_STEAL_BATCH=1 turns it back off.

Run directly:  PYTHON_GIL=0 PYTHONPATH=src python tests/test_steal_batch.py
"""
import json
import os
import subprocess
import sys

import pytest

import runloom
import runloom_c as rc

from adv_util import needs_free_threading

pytestmark = pytest.mark.skipif(not needs_free_threading(),
                                reason="M:N hubs need a free-threaded build")

N = 4096


def _cover(name):
    if not rc._cover_enabled():
        return None
    return rc._cover_report().get(name, 0)


def bulk_spawn_from_one_fiber(n=N, hubs=2):
    """One fiber spawns n fresh fibers in a tight loop.  Placement is
    round-robin, so half land on the spawner's own hub -- whose only pick
    steps are the auto-pace yields, and the ready ring (where the yielded
    spawner sits) is served before the deque, so that half PILES UP on its
    deque while the other hub drains its own share and then steals.
    Fresh, unpinned gs: identical in default and migration mode.
    Returns (per-fiber results, stats)."""
    out = rc.Chan(n)

    def body(i):
        def run():
            acc = i
            for k in range(20):
                acc = (acc * 1103515245 + k) & 0xFFFFFFFF
            out.send((i, rc.mn_current_hub(), acc))
        return run

    def spawner():
        for i in range(n):
            rc.mn_fiber(body(i))

    if rc._cover_enabled():
        rc._cover_reset()
    rc.mn_init(hubs)
    rc.mn_fiber(spawner)
    rc.mn_run()
    stats = rc.stats()
    rc.mn_fini()
    res = [out.try_recv()[0] for _ in range(n)]
    return res, stats


def test_bulk_spawn_batch_runs_every_fiber_exactly_once():
    res, stats = bulk_spawn_from_one_fiber()
    seen = {}
    for i, hub, acc in res:
        assert i not in seen, f"fiber {i} ran twice"
        seen[i] = hub
    assert len(seen) == N
    assert stats["mn_pending_total"] == 0
    assert stats["mn_deque_depth"] == 0
    hits = _cover("steal_batch")
    if hits is not None:
        assert _cover("steal_hit") > 0, "the idle hub never stole from the spawner's deque"
        assert hits > 0, "thieves only ever took one g per pick step"


def _worker(mode):
    """Subprocess body: run one workload and print the cover report as JSON."""
    if mode == "bulk":
        res, stats = bulk_spawn_from_one_fiber()
        assert len({i for i, _, _ in res}) == N
    elif mode == "burst":
        # Migration mode: one hub-thread waker wakes 256 parked fibers
        # back-to-back.  Local wake puts them all on the waker's deque; the
        # other hub must take them in batches, not one per pick step.
        n = 256
        handoff, done = rc.Chan(n), rc.Chan(n)

        def sleeper(i):
            def body():
                handoff.send(rc.current_g())
                rc.park()
                done.send(i)
            return body

        def waker():
            gs = [handoff.recv()[0] for _ in range(n)]
            for g in gs:
                while g.stack()["state"] != "parked":
                    rc.yield_()
            for g in gs:
                g.wake()

        if rc._cover_enabled():
            rc._cover_reset()
        rc.mn_init(2)
        rc.mn_fiber(waker)
        for i in range(n):
            rc.mn_fiber(sleeper(i))
        rc.mn_run()
        stats = rc.stats()
        rc.mn_fini()
        assert sorted(done.try_recv()[0] for _ in range(n)) == list(range(n))
    else:
        raise SystemExit("unknown mode %s" % mode)
    assert stats["mn_pending_total"] == 0
    rep = rc._cover_report() if rc._cover_enabled() else {}
    print(json.dumps({"cover": rep, "migration": runloom.migration_available()
                      and runloom.migration_enabled()}))


def _run_worker(mode, **env):
    e = dict(os.environ, PYTHON_GIL="0", **env)
    e["PYTHONPATH"] = os.pathsep.join(
        p for p in (os.environ.get("PYTHONPATH", ""),
                    os.path.join(os.path.dirname(__file__), "..", "src")) if p)
    r = subprocess.run([sys.executable, __file__, "--worker", mode], env=e,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_env_knob_restores_single_item_steal():
    doc = _run_worker("bulk", RUNLOOM_STEAL_BATCH="1")
    if doc["cover"]:
        assert doc["cover"].get("steal_hit", 0) > 0
        assert doc["cover"].get("steal_batch", 0) == 0


@pytest.mark.skipif(not runloom.migration_available(),
                    reason="needs a free-threaded build carrying both CPython migration patches")
def test_wake_burst_under_migration_spreads_in_batches():
    doc = _run_worker("burst", RUNLOOM_MIGRATION="1")
    assert doc["migration"]
    if doc["cover"]:
        assert doc["cover"].get("local_wake", 0) > 0
        assert doc["cover"].get("steal_batch", 0) > 0, \
            "the idle hub only ever took one woken g per pick step"


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        _worker(sys.argv[2])
    else:
        sys.exit(pytest.main([__file__, "-v"]))
