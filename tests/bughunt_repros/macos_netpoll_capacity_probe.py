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

Three variants distinguish the causes.  Run under the target interpreter:

    PYTHON_GIL=0 python tests/bughunt_repros/macos_netpoll_capacity_probe.py

  baseline   -- the failing snippet verbatim.  Reproduces on macOS, passes on
                Linux (verified), so this confirms we are looking at the same
                thing CI sees.
  preinit    -- call rc.netpoll_poll() FIRST, which routes through
                runloom_netpoll_init() and therefore runloom_fd_arrays_init().
                If baseline FAILS and this PASSES, the arrays were simply not
                allocated yet: an init-ordering bug, and the fix belongs on the
                sim-delivery path (init before stashing), not in sizing.
  maxfd      -- baseline with RUNLOOM_NETPOLL_MAXFD forced.  Only informative
                if it changes anything: it would mean the capacity was computed
                (not skipped) and merely came out too small, which would point
                at runloom_fd_cap_target() and NOT at ordering.

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

VARIANTS = [
    ("baseline", "", {}),
    # netpoll_poll -> runloom_netpoll_pump -> runloom_netpoll_init ->
    # runloom_fd_arrays_init.  Brings the per-fd arrays up before any sim wake.
    ("preinit", "rc.netpoll_poll()", {}),
    ("maxfd", "", {"RUNLOOM_NETPOLL_MAXFD": "65536"}),
]


def run(name, preamble, extra_env):
    env = dict(os.environ, PYTHON_GIL="0", RUNLOOM_GIL="0")
    env["PYTHONPATH"] = os.path.join(REPO, "src") + os.pathsep + env.get("PYTHONPATH", "")
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
    print("READING: baseline FAIL + preinit PASS  -> init-ordering bug "
          "(arrays not allocated when the sim wake was stashed)")
    print("         baseline FAIL + preinit FAIL  -> not ordering; look at "
          "the kqueue delivery path itself")
    print("         maxfd changing anything       -> sizing, not ordering")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
