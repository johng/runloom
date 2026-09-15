#!/usr/bin/env python3
"""Re-print saved workflow results with ratio columns against chosen baselines.

    python report.py results/workflows.json results/hubs_*.json
    python report.py --baselines threads,asyncio results/*.json   (default)
    python report.py --baselines go results/workflows.json

For each row: median ops/s per runtime, then each runtime as a MULTIPLE of
each baseline (>1 = faster than the baseline).  Memory: rss delta and
bytes/unit, then the same ratio blocks (<1 = less memory than the baseline).
"""
import argparse
import json
import re
import sys


def fmt_rate(x):
    if x >= 1e9:
        return "%.2fG" % (x / 1e9)
    if x >= 1e6:
        return "%.2fM" % (x / 1e6)
    if x >= 1e3:
        return "%.1fk" % (x / 1e3)
    return "%.0f" % x


def fmt_bytes(x):
    if x >= 1 << 30:
        return "%.2fG" % (x / (1 << 30))
    if x >= 1 << 20:
        return "%.1fM" % (x / (1 << 20))
    if x >= 1 << 10:
        return "%.0fk" % (x / (1 << 10))
    return "%.0f" % x


def ratio_cols(runtimes, baselines):
    return [(r, b) for b in baselines for r in runtimes if r != b]


def row_key(row):
    """Natural order: workload, then n=, cpu=, @workers numerically."""
    nums = [int(x) for x in re.findall(r"(?:n=|cpu=|job=|@)(\d+)", row)]
    return (re.split(r"[ @]", row)[0], nums)


def block(table, rows, runtimes, baselines, key, fmt, title, lower_is_better=False):
    w, wr = 12, 9
    hdr = "%-24s" % title + "".join("%*s" % (w, r) for r in runtimes)
    hdr += "  |" + "".join("%*s" % (wr + len(b) + 1, "x%s" % b) for _, b in ratio_cols(runtimes, baselines))
    print(hdr)
    sub = " " * 24 + " " * (w * len(runtimes)) + "  |" + "".join(
        "%*s" % (wr + len(b) + 1, r) for r, b in ratio_cols(runtimes, baselines))
    print(sub)
    for row in rows:
        cells = table[row]
        line = "%-24s" % row
        for r in runtimes:
            c = cells.get(r)
            line += "%*s" % (w, fmt(c[key]) if c and key in c else (c or {}).get("error", "-")[:w - 1])
        line += "  |"
        for r, b in ratio_cols(runtimes, baselines):
            c, bc = cells.get(r), cells.get(b)
            if c and bc and key in c and key in bc and bc[key]:
                line += "%*s" % (wr + len(b) + 1, "%.2f" % (c[key] / bc[key]))
            else:
                line += "%*s" % (wr + len(b) + 1, "-")
        print(line)
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--baselines", default="threads,asyncio")
    ap.add_argument("--no-mem", action="store_true")
    a = ap.parse_args()
    baselines = [b for b in a.baselines.split(",") if b]
    for path in a.files:
        doc = json.load(open(path))
        table = doc["results"]
        rows = sorted(table, key=row_key)
        runtimes = []
        for row in rows:
            for r in table[row]:
                if r not in runtimes:
                    runtimes.append(r)
        order = ["asyncio", "threads", "threads-sq", "runloom", "runloom-mig", "go"]
        runtimes.sort(key=lambda r: order.index(r) if r in order else 99)
        bl = [b for b in baselines if b in runtimes]
        env = doc.get("env", {})
        print("=== %s  (%s, %s workers, %s samples)" % (
            path, env.get("machine", "?"), env.get("workers", "?"), env.get("samples", "?")))
        block(table, rows, runtimes, bl, "ops_per_s", fmt_rate, "ops/s")
        if not a.no_mem:
            block(table, rows, runtimes, bl, "rss_delta_bytes", fmt_bytes, "rss delta")
            block(table, rows, runtimes, bl, "bytes_per_unit", fmt_bytes, "bytes/unit")


if __name__ == "__main__":
    main()
