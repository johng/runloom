"""Discriminate WHY macOS drops a sim-delivered wake: capacity 0, or no init?

On macos-14 CI both interpreters fail
test_mn_sim_bytes::TestReviewRegressions::test_late_parker_gets_stashed_wake
with the receiver never seeing its byte, and this on stderr:

    [runloom] fd 4 exceeds preallocated netpoll capacity 0; raise
              RUNLOOM_NETPOLL_MAXFD (event dropped)
    OSError: [Errno 89] Operation canceled          <- ECANCELED (125 on Linux)

Capacity 0 cannot come from sizing.  runloom_fd_cap_target() starts at 65536,
prefers RLIMIT_NOFILE, and clamps to [1024, 8M] -- so even macOS's stingy
256-fd rlim_cur floors to 1024.  Capacity is 0 only if runloom_fd_arrays_init()
never ran, or its ~25 MB of calloc failed (implausible on a runner).  And that
function is called from exactly ONE place: runloom_netpoll_init().

So the hypothesis is an INIT-ORDERING bug on the kqueue path -- the sim wake is
stashed before netpoll has been brought up, the per-fd arrays do not exist, the
event is dropped, and the parker is cancelled.

The test runs the snippet in SIMULATION mode (RUNLOOM_SIM=1, RUNLOOM_SIM_MN=1
via mn_digest.hermetic_env), and that matters: the first version of this probe
omitted it, exercised the REAL netpoll path, and PASSED on macos-14 while the
test failed 5/5 on the same runner.  A probe that does not carry the failing
configuration only measures itself.

Four variants, run under the target interpreter:

    PYTHON_GIL=0 python tests/bughunt_repros/macos_netpoll_capacity_probe.py

  real        -- real netpoll. Control; expected to pass everywhere.
  sim         -- the configuration under test. Expected to reproduce
                 "capacity 0 -> event dropped -> ECANCELED" on macOS.
  sim+preinit -- rc.netpoll_poll() first, which routes through
                 runloom_netpoll_init() -> runloom_fd_arrays_init(). If `sim`
                 FAILS and this PASSES, the arrays simply were not allocated
                 yet: an ordering bug, fixed on the sim-delivery path.
  sim+maxfd   -- sim with RUNLOOM_NETPOLL_MAXFD forced. Only informative if it
                 changes the outcome: that would mean capacity was computed and
                 merely too small (sizing), not skipped (ordering).

Exit 0 if every variant delivered the byte; 1 otherwise.  The point is the
per-variant table, not the exit code.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The CI snippet, verbatim from test_mn_sim_bytes, with an optional preamble.
BODY = """
import socket, runloom_c as rc
%(preamble)s
a, b = socket.socketpair()
a.setblocking(False); b.setblocking(False)
cid = rc.sim_conn_register(a.fileno(), b.fileno())
got = {}
def sender():
    a.send(b'x')
    rc.sim_deliver_ready(cid, b.fileno(), 1)
def receiver():
    rc.sched_sleep(0.001)
    rc.wait_fd(b.fileno(), 1)
    got['d'] = b.recv(16)
rc.mn_init(2)
rc.mn_fiber(receiver)
rc.mn_fiber(sender)
rc.mn_run(); rc.mn_fini()
print('LATE_PARKER', got.get('d'))
print('BACKEND', rc.netpoll_backend())
"""

# The test runs this in SIMULATION mode -- test_mn_sim_bytes sets
#   SIM_ENV = {"RUNLOOM_SIM": "1", "RUNLOOM_SIM_MN": "1"}
# and passes it through mn_digest.hermetic_env (which strips every inherited
# RUNLOOM_* knob first, then pins PYTHON_GIL / PYTHONHASHSEED / PYTHONPATH).
# The FIRST version of this probe omitted that and therefore exercised the REAL
# netpoll path: it passed on macos-14 (run 33943464165) while the test failed
# 5/5 there, which told us only that the probe was wrong.  Sim mode is the
# configuration under test, so every meaningful variant carries it.
SIM = {"RUNLOOM_SIM": "1", "RUNLOOM_SIM_MN": "1", "RUNLOOM_MN_SEED": "12345"}


def _sim(**extra):
    e = dict(SIM)
    e.update(extra)
    return e


VARIANTS = [
    # Control: the real netpoll path. Expected to pass everywhere; if it ever
    # fails, the problem is not sim-specific and this file is the wrong hunt.
    ("real", "", {}),
    # The configuration the test actually uses. This is the one expected to
    # reproduce "capacity 0 -> event dropped -> ECANCELED" on macOS.
    ("sim", "", _sim()),
    # netpoll_poll -> runloom_netpoll_pump -> runloom_netpoll_init ->
    # runloom_fd_arrays_init: brings the per-fd arrays up BEFORE any sim wake.
    ("sim+preinit", "rc.netpoll_poll()", _sim()),
    # Only informative if it changes the outcome: that would mean capacity was
    # computed and merely too small (sizing), not skipped (ordering).
    ("sim+maxfd", "", _sim(RUNLOOM_NETPOLL_MAXFD="65536")),
]


def run(name, preamble, extra_env):
    # Mirror hermetic_env: strip inherited RUNLOOM_* so the parent's knobs
    # cannot contaminate the child, then pin exactly what the test pins.
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUNLOOM_")}
    env["PYTHON_GIL"] = "0"
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONPATH"] = os.path.join(REPO, "src")
    env.update(extra_env)
    p = subprocess.run([sys.executable, "-c", BODY % {"preamble": preamble}],
                       cwd=REPO, env=env, timeout=120,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.stdout or "", p.stderr or ""
    delivered = "LATE_PARKER b'x'" in out
    backend = next((l.split(" ", 1)[1] for l in out.splitlines()
                    if l.startswith("BACKEND ")), "?")
    cap0 = "preallocated netpoll capacity 0" in err
    cancel = "Operation canceled" in err or "Errno 89" in err or "Errno 125" in err
    print("  %-9s delivered=%-5s backend=%-8s capacity0=%-5s cancelled=%-5s rc=%d"
          % (name, delivered, backend, cap0, cancel, p.returncode))
    for line in err.strip().splitlines()[:4]:
        print("             | %s" % line)
    return delivered


def main():
    print("macos_netpoll_capacity_probe: %s" % sys.executable)
    ok = True
    for name, preamble, extra_env in VARIANTS:
        try:
            ok &= run(name, preamble, extra_env)
        except subprocess.TimeoutExpired:
            print("  %-9s TIMEOUT" % name)
            ok = False
    print()
    print("READING:")
    print("  real PASS + sim FAIL          -> the bug is in the SIM delivery")
    print("                                   path, not real netpoll.")
    print("  sim FAIL + sim+preinit PASS   -> init-ordering: the per-fd arrays")
    print("                                   were not allocated when the sim")
    print("                                   wake was stashed. Fix = init")
    print("                                   before stashing.")
    print("  sim FAIL + sim+preinit FAIL   -> not ordering; the kqueue sim")
    print("                                   delivery path itself.")
    print("  sim+maxfd changing anything   -> sizing, not ordering.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
