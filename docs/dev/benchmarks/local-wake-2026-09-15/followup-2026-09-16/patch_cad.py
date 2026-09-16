"""Experiment: make the busy-hub self-pump cadence (every 64 loop turns)
tunable via RUNLOOM_SELF_PUMP_MASK so we can test whether the 2-hub mixed
loss under local wake is wake LATENCY on the busy hub's private kqueue."""
import pathlib

p = pathlib.Path("/private/tmp/claude-501/-Users-johng-repos-runloom/005e6dab-cfdb-4cee-850c-efac05d80922/scratchpad/expsrc2/src/runloom_c/mn_sched_hub_main.c.inc")
s = p.read_text()
old = "        if ((++self_pump_ctr & 0x3fu) == 0) (void)runloom_netpoll_pump(0);\n"
new = ("        {\n"
       "            static unsigned mask = 0xffffffffu;\n"
       "            if (mask == 0xffffffffu) {\n"
       "                const char *e = getenv(\"RUNLOOM_SELF_PUMP_MASK\");\n"
       "                mask = (e && e[0]) ? (unsigned)atoi(e) : 0x3fu;\n"
       "            }\n"
       "            if ((++self_pump_ctr & mask) == 0) (void)runloom_netpoll_pump(0);\n"
       "        }\n")
assert s.count(old) == 1
p.write_text(s.replace(old, new))
print("patched")
