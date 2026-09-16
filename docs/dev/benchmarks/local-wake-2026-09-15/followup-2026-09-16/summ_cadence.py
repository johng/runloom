"""Summarise cadence_bench.log: median ops/s per (mode, workload, hubs, turns)."""
import re
import statistics
import sys
from collections import defaultdict

runs = defaultdict(list)
key = None
for line in open(sys.argv[1]):
    m = re.match(r"-- mig=(\d) wl=(\w+) hubs=(\d+) turns=(\d+)", line)
    if m:
        key = (int(m[1]), m[2], int(m[3]), int(m[4]))
        continue
    m = re.search(r"ops/s=(\d+)", line)
    if m and key:
        runs[key].append(int(m[1]))
print("| mode | workload | hubs | turns=64 | turns=16 | change |")
print("| --- | --- | ---: | ---: | ---: | ---: |")
for mode in (1, 0):
    for wl in ("mixed", "fanout_io"):
        for hubs in (2, 8, 18):
            a = runs.get((mode, wl, hubs, 64), [])
            b = runs.get((mode, wl, hubs, 16), [])
            if not a or not b:
                continue
            ma, mb = statistics.median(a), statistics.median(b)
            fa = " / ".join("%.1fk" % (x / 1e3) for x in a)
            fb = " / ".join("%.1fk" % (x / 1e3) for x in b)
            print("| %s | %s | %d | %.1fk (%s) | %.1fk (%s) | %+.1f%% |"
                  % ("migration" if mode else "default", wl, hubs, ma / 1e3, fa,
                     mb / 1e3, fb, 100 * (mb / ma - 1)))
