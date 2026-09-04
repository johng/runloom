"""Dedicated offload hubs (RUNLOOM_OFFLOAD_HUBS).

The mechanism: reserve K hubs at the tail of runloom_hubs[], exclude them from
general placement / stealing / sysmon preemption, and run blocking calls there
as ordinary fibers.  That lets `offload` reuse the scheduler (spawn, submit,
channel, wake_g) instead of the bespoke thread pool + self-pipe + result-box
protocol, and -- because nothing ever migrates between hubs -- it needs no
CPython tstate patches.  See the RUNLOOM_OFFLOAD_HUBS block in mn_sched.c.

The flag is resolved ONCE per process (before any hub starts), so each case
runs in its own subprocess rather than trying to re-init in-process.

`time.sleep` is the stand-in for a blocking call: it releases the tstate, so a
hub sitting in it is DETACHED-with-work, exactly like a blocking syscall.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")


def _run_case(body, env_extra=None, timeout=90):
    """Run `body` as a standalone program; return (rc, stdout+stderr)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHON_GIL"] = "0"
    env.setdefault("RUNLOOM_SYSMON_QUIET", "1")
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, "-c", body], env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# --------------------------------------------------------------------------
# Default OFF: the feature must be invisible.

_OFF = r"""
import runloom, runloom_c as rc
def body():
    assert rc.offload_hub_count() == 0, rc.offload_hub_count()
    assert rc.mn_hub_count() == 4, rc.mn_hub_count()
    try:
        rc.offload_fiber(lambda: None)
    except RuntimeError:
        print("RAISED_OK")
    else:
        raise AssertionError("offload_fiber must refuse a general hub fallback")
runloom.run(4, body)
print("OK")
"""


def test_default_off_is_invisible():
    """No env var -> no offload hubs, hub count unchanged, and offload_fiber
    REFUSES rather than silently falling back to a general hub (a blocking call
    there would strand every g woken on it)."""
    rc, out = _run_case(_OFF)
    assert rc == 0, out
    assert "RAISED_OK" in out and "OK" in out, out


# --------------------------------------------------------------------------
# Reservation: K hubs are ADDED, not carved out of the general pool.

_RESERVE = r"""
import runloom, runloom_c as rc
def body():
    assert rc.offload_hub_count() == 3, rc.offload_hub_count()
    # 4 general + 3 offload: enabling the feature must not shrink general
    # parallelism, so the total grows.
    assert rc.mn_hub_count() == 7, rc.mn_hub_count()
runloom.run(4, body)
print("OK")
"""


def test_offload_hubs_are_added_not_carved_out():
    rc, out = _run_case(_RESERVE, {"RUNLOOM_OFFLOAD_HUBS": "3"})
    assert rc == 0, out
    assert "OK" in out, out


# --------------------------------------------------------------------------
# EXCLUSION 1 -- general spawns never land on an offload hub.
#
# Oracle without a current-hub API: saturate every offload hub with a long
# blocking call, then spawn a batch of ordinary fibers that only increment and
# exit.  A general fiber placed on (or stolen onto) an offload hub could not
# run until the block cleared, so "all of them finished well before the block
# ended" is exactly the exclusion holding.

_EXCL = r"""
import time
import runloom, runloom_c as rc

K, N, BLOCK = 2, 400, 3.0
done = [0] * N
started = time.monotonic()

def body():
    def blocker():
        time.sleep(BLOCK)          # releases the tstate: a real blocking call
    for _ in range(K):
        rc.offload_fiber(blocker)
    runloom.sleep(0.25)            # let every offload hub get into its sleep

    def worker(i):
        def f():
            done[i] = 1
        return f
    for i in range(N):
        runloom.fiber(worker(i))

    # Poll rather than sleep a fixed time, so a fast machine is not penalised.
    deadline = started + BLOCK - 0.5
    while time.monotonic() < deadline and sum(done) < N:
        runloom.sleep(0.02)

    got = sum(done)
    elapsed = time.monotonic() - started
    print("done=%d/%d elapsed=%.2f block=%.1f" % (got, N, elapsed, BLOCK))
    assert got == N, "%d/%d general fibers stalled -- one landed on a blocked offload hub" % (got, N)
    assert elapsed < BLOCK, "general work waited for the offload block to clear"
runloom.run(4, body)
print("OK")
"""


def test_general_fibers_never_land_on_a_blocked_offload_hub():
    rc, out = _run_case(_EXCL, {"RUNLOOM_OFFLOAD_HUBS": "2"})
    assert rc == 0, out
    assert "OK" in out, out


# --------------------------------------------------------------------------
# The point of the whole thing: general hubs keep making progress while EVERY
# offload hub is blocked.  This is what the thread pool bought before, now
# bought with the scheduler instead.

_PROGRESS = r"""
import time
import runloom, runloom_c as rc

K, BLOCK = 2, 2.0
ticks = bytearray(4)          # one slot per ticker: a shared += loses GIL-off

def body():
    def blocker():
        time.sleep(BLOCK)
    for _ in range(K):
        rc.offload_fiber(blocker)
    runloom.sleep(0.25)

    stop = [False]
    def ticker(i):
        def f():
            n = 0
            while not stop[0]:
                n = (n + 1) % 200
                ticks[i] = 1 if n else ticks[i]
                runloom.sleep(0.01)
        return f
    for i in range(4):
        runloom.fiber(ticker(i))

    runloom.sleep(1.0)         # a full second WHILE the offload hubs are blocked
    stop[0] = True
    runloom.sleep(0.1)
    live = sum(ticks)
    print("tickers_alive=%d/4" % live)
    assert live == 4, "only %d/4 general tickers ran while offload hubs blocked" % live
runloom.run(4, body)
print("OK")
"""


def test_general_hubs_progress_while_offload_hubs_block():
    rc, out = _run_case(_PROGRESS, {"RUNLOOM_OFFLOAD_HUBS": "2"})
    assert rc == 0, out
    assert "OK" in out, out


# --------------------------------------------------------------------------
# Results come back the ordinary way -- a channel, not a bespoke completion
# protocol.  This is the simplification the design exists for.

_RESULT = r"""
import time
import runloom, runloom_c as rc

def body():
    ch = runloom.Chan(1)
    def blocker():
        time.sleep(0.3)
        ch.send(("answer", 42))
    rc.offload_fiber(blocker)
    tag, val = ch.recv()        # normal channel recv; caller parked on its own hub
    assert (tag, val) == ("answer", 42), (tag, val)
    print("got %s=%d" % (tag, val))
runloom.run(4, body)
print("OK")
"""


def test_result_returns_over_a_normal_channel():
    rc, out = _run_case(_RESULT, {"RUNLOOM_OFFLOAD_HUBS": "1"})
    assert rc == 0, out
    assert "OK" in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
