"""Experiment: route wakes performed INSIDE the netpoll pump to the global
run-queue (Go's injectglist), keeping the local deque only for wakes done by
a running fiber.  Applied to the scratch snapshot only."""
import pathlib

root = pathlib.Path("/private/tmp/claude-501/-Users-johng-repos-runloom/005e6dab-cfdb-4cee-850c-efac05d80922/scratchpad/expsrc/src/runloom_c")

p = root / "mn_sched.c"
s = p.read_text()
old = "static void runloom_mn_woken_enqueue(runloom_g_t *g);\n"
assert s.count(old) == 1
s = s.replace(old, old + "static __thread int runloom_tls_in_pump;\n")
p.write_text(s)

p = root / "mn_sched_mn_api.c.inc"
s = p.read_text()
old = "    if (cur != NULL && runloom_hubs != NULL &&\n        !runloom_hub_is_offload(cur->id) &&"
assert s.count(old) == 1, "woken_enqueue condition"
s = s.replace(old, "    if (cur != NULL && runloom_hubs != NULL &&\n        runloom_tls_in_pump == 0 &&\n        !runloom_hub_is_offload(cur->id) &&")
p.write_text(s)

p = root / "mn_sched_hub_main.c.inc"
s = p.read_text()
reps = [
    ("        if ((++self_pump_ctr & 0x3fu) == 0) (void)runloom_netpoll_pump(0);\n",
     "        if ((++self_pump_ctr & 0x3fu) == 0) {\n            runloom_tls_in_pump = 1; (void)runloom_netpoll_pump(0); runloom_tls_in_pump = 0;\n        }\n"),
    ("                        runloom_netpoll_pump(0);\n                    }\n                    (void)lflags;\n",
     "                        runloom_tls_in_pump = 1; runloom_netpoll_pump(0); runloom_tls_in_pump = 0;\n                    }\n                    (void)lflags;\n"),
    ("                        runloom_netpoll_pump(pump_ns);\n                        runloom_mnwake_trace_event(\"HUB_UNBLOCK\", 0, 0);\n",
     "                        runloom_tls_in_pump = 1; runloom_netpoll_pump(pump_ns); runloom_tls_in_pump = 0;\n                        runloom_mnwake_trace_event(\"HUB_UNBLOCK\", 0, 0);\n"),
]
for old, new in reps:
    assert s.count(old) == 1, old
    s = s.replace(old, new)
p.write_text(s)
print("patched")
