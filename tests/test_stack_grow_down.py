"""Tests for the function-bound stack grow-down (runloom.set_grow_down).

The grow-down is the default-on, M:N-only auto-sizer: each fiber starts at
the fixed default stack ("cold start"), measures its real C-stack high-water-mark
on return, and writes a derived, smaller size back onto the callable itself
(fn.__dict__[GROW_DOWN_KEY] = [learned_bytes, spawns_measured]).  The next spawn
of that function reserves only next_pow2(hwm * MARGIN).

The observable is that learned store: a function spawned under run(n>1) ends up
with a learned size well below the default, floored at GROW_DOWN_MIN, while
run(1) / an explicit stack_size= / disabling / the opt-in C autosizer all leave
it untouched.
"""
import json
import os
import re as _re
import subprocess as _subprocess
import sys as _sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import runloom
import runloom_c
from runloom.runtime import GROW_DOWN_KEY, GROW_DOWN_MIN, GROW_DOWN_SAMPLES

# Stack high-water-mark is precise only on a POSIX guard-page backend
# (fcontext-asm / ucontext) with 4 KB pages -- see test_stack_autosize.py.
_RELIABLE_HWM = (os.name == "posix"
                 and runloom_c.backend() in ("fcontext-asm", "ucontext")
                 and os.sysconf("SC_PAGESIZE") == 4096)
pytestmark = pytest.mark.skipif(
    not _RELIABLE_HWM,
    reason="stack HWM is reliable only on a POSIX guard-page backend with 4 KB pages")

# The static _RELIABLE_HWM gate above predicts the "reports resident, not
# touched" mincore failure from backend and page size; hosted runners hit it
# anyway with 4 KB pages (test_stack_frames.py learned this the same way).
# Here the tolerance is far tighter than there, so that file's "> half the
# allocation" sentinel cannot detect it:
#
#   learned = next_pow2(hwm * GROW_DOWN_MARGIN), floored at GROW_DOWN_MIN
#           = next_pow2(hwm * 4), floor 16384
#
# so the floor assertions hold ONLY if a do-nothing fiber measures exactly ONE
# page: 4096*4 == 16384 -> the floor, but 8192*4 == 32768 -> one doubling above
# it.  A single page of residency noise is the entire budget, and that is what
# CI reported (run 33939258491, 3.14.4/ubuntu at j=4: `assert 32768 == 16384`)
# while the identical code passed on the same runner at j=2 and passes locally,
# where the probe measures 4096 on the nose.
#
# So ask rather than predict, and calibrate the question to what this file
# needs: does a do-nothing fiber measure one page?  If not, the probe is
# over-reporting and the floor assertions cannot mean anything.  Only those two
# assertions are gated; every other test here reads the learner's BEHAVIOUR
# (store present/absent, explicit size wins, run(1) does not learn, freezing
# after GROW_DOWN_SAMPLES) rather than the measured number, so they stay live.
_HWM_OVERREPORTS = None   # None = not probed yet; True = probe unusable here


def _hwm_overreports_a_light_fiber():
    """True if this host's HWM probe cannot resolve a do-nothing fiber to one
    page -- measured in a CLEAN subprocess, because stats()['stack_hwm'] is a
    process-global maximum that any earlier fiber in this process would
    pollute."""
    global _HWM_OVERREPORTS
    if _HWM_OVERREPORTS is None:
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import runloom_c\n"
            "def worker():\n"
            "    pass\n"
            "runloom_c.fiber(worker, stack_size=%d)\n"
            "runloom_c.run()\n"
            "print('HWM', runloom_c.stats().get('stack_hwm', 0))\n"
            % (os.path.join(_REPO, "src"), 512 * 1024)
        )
        env = dict(os.environ, PYTHON_GIL="0", RUNLOOM_GIL="0")
        try:
            p = _subprocess.run([_sys.executable, "-c", code], cwd=_REPO, env=env,
                                timeout=60, stdout=_subprocess.PIPE,
                                stderr=_subprocess.PIPE, text=True)
            m = _re.search(r"HWM (\d+)", p.stdout or "")
            # A `pass` body cannot touch beyond the one page its entry frame
            # lands on.  Anything above that is residency, not usage.
            _HWM_OVERREPORTS = bool(m) and int(m.group(1)) > 4096
        except Exception:
            _HWM_OVERREPORTS = False   # cannot tell -> assume usable, fail loudly
    return _HWM_OVERREPORTS


def _skip_unless_hwm_resolves_one_page():
    if _hwm_overreports_a_light_fiber():
        pytest.skip("HWM probe over-reports a do-nothing fiber (>1 page) on "
                    "this host: next_pow2(hwm*4) cannot land on the floor, so "
                    "this measurement is invalid here -- not a runloom defect")

# 80-deep nested list -> json.dumps recurses ~14 KiB of real C stack.
NESTED = []
_cur = NESTED
for _ in range(80):
    _nx = []
    _cur.append(_nx)
    _cur = _nx


@pytest.fixture(autouse=True)
def _clean():
    runloom.set_grow_down(True)
    runloom.inspect.enable_stack_autosize(False)
    yield
    runloom.set_grow_down(True)
    runloom.inspect.enable_stack_autosize(False)


def _spawn_mn(fn, n, hubs=4, **go_kw):
    """Spawn fn n times under run(hubs); mn_run joins them all before returning."""
    def main():
        for _ in range(n):
            runloom.fiber(fn, **go_kw)
    runloom.run(hubs, main)


def test_on_by_default():
    assert runloom.grow_down_enabled() is True


def test_light_learns_to_floor():
    def worker():
        return 1
    _spawn_mn(worker, 80)
    store = worker.__dict__.get(GROW_DOWN_KEY)
    # The learner ran and wrote a store: behaviour, not measurement, so this
    # holds even where the probe is untrustworthy.
    assert store is not None
    _skip_unless_hwm_resolves_one_page()
    # a do-nothing fiber touches ~1 page -> shrinks to the floor
    assert store[0] == GROW_DOWN_MIN


def test_deep_learns_a_real_size_below_default():
    default = runloom_c.get_stack_size()
    def worker():
        json.dumps(NESTED)       # ~14 KiB of real C stack
    _spawn_mn(worker, 80)
    learned = worker.__dict__.get(GROW_DOWN_KEY)[0]
    # learned a size that covers the real HWM with margin, still well under the
    # default cold start (the whole point: reserve what's needed, not 512 KiB)
    assert GROW_DOWN_MIN <= learned < default
    assert learned & (learned - 1) == 0    # power of two
    assert learned >= 32 * 1024            # covers ~14 KiB * 4 margin


def test_n1_does_not_learn():
    # single-thread run(1) keeps the fixed default -- no learning, no store
    def worker():
        json.dumps(NESTED)
    runloom.run(1, lambda: [runloom.fiber(worker) for _ in range(40)])
    assert worker.__dict__.get(GROW_DOWN_KEY) is None


def test_disable_bypasses():
    runloom.set_grow_down(False)
    def worker():
        return 1
    _spawn_mn(worker, 40)
    assert worker.__dict__.get(GROW_DOWN_KEY) is None
    assert runloom.grow_down_enabled() is False


def test_explicit_pin_bypasses():
    def worker():
        return 1
    _spawn_mn(worker, 40, stack_size=128 * 1024)
    assert worker.__dict__.get(GROW_DOWN_KEY) is None


def test_defers_to_c_autosizer_when_enabled():
    def worker():
        return 1
    def main():
        runloom.inspect.enable_stack_autosize(True)
        for _ in range(40):
            runloom.fiber(worker)
    runloom.run(4, main)
    # the explicitly-enabled C autosizer wins; grow-down backs off entirely
    assert worker.__dict__.get(GROW_DOWN_KEY) is None


def test_freezes_after_samples():
    # spawn far more than GROW_DOWN_SAMPLES; the measured/wrapped count is capped,
    # so the steady state stops paying the per-completion measurement
    def worker():
        return 1
    total = GROW_DOWN_SAMPLES + 200
    _spawn_mn(worker, total)
    store = worker.__dict__.get(GROW_DOWN_KEY)
    assert store is not None
    # froze: only ~GROW_DOWN_SAMPLES spawns were ever wrapped (a small concurrent
    # overshoot is fine), nowhere near the full `total`.  This is the actual
    # subject of the test and is measurement-independent, so assert it FIRST --
    # a bad probe must not cost us the freezing coverage.
    assert store[1] <= GROW_DOWN_SAMPLES + 8
    assert store[1] < total
    _skip_unless_hwm_resolves_one_page()
    assert store[0] == GROW_DOWN_MIN


def test_non_introspectable_callable_is_safe():
    # a callable with no writable __dict__ (slots) can't carry a learned size;
    # it must fall back to the cold start without crashing
    class SlotCallable:
        __slots__ = ()
        def __call__(self):
            return 1
    c = SlotCallable()
    assert getattr(c, "__dict__", None) is None
    _spawn_mn(c, 10)     # must not raise


def test_arg_bearing_binds_to_real_function():
    # runloom.fiber(fn, arg) wraps fn in an arg-binding lambda; the learned size must
    # bind to fn (shared across all arg variants), not the per-call wrapper
    def worker(x):
        json.dumps(NESTED)
        return x
    def main():
        for i in range(80):
            runloom.fiber(worker, i)
    runloom.run(4, main)
    store = worker.__dict__.get(GROW_DOWN_KEY)
    assert store is not None and store[0] >= 32 * 1024
