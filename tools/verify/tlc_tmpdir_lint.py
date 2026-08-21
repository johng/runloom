#!/usr/bin/env python3
"""tlc_tmpdir_lint.py -- every TLC invocation must carry -Djava.io.tmpdir.

WHY THIS IS A LINT AND NOT A COMMENT.  tla2tools resolves the standard modules
a spec EXTENDS (Naturals, Sequences, FiniteSets) out of the jar and materialises
each at a FIXED, unqualified path -- java.io.tmpdir + "/" + "Naturals.tla", no
pid, no random suffix -- with a truncating FileOutputStream, then deleteOnExit().
Two TLC JVMs sharing /tmp therefore truncate and delete each other's copies
mid-parse.  That is tlaplus/tlaplus#688; fixed on upstream master, in no release
as of v1.7.4, so we work around it by giving every JVM a private tmpdir.

The failure it causes is a SANY parse abort, which -- because every caller pipes
run_tlc into `grep -q` -- is indistinguishable from a negative control that has
stopped detecting its injected bug.  It ran at ~1 in 40 verify-fast runs and
cost a full session to diagnose.  A prose warning in tools/verify/tla/README.md
does not stop someone dropping the flag as tidying; this does.

Scope: shell and Python sources only.  Markdown is deliberately NOT scanned --
tools/verify/tla/README.md quotes the UNSAFE invocation on purpose, to
demonstrate the symlink-overwrite consequence.

Exit 0 = every invocation carries the flag.  Exit 1 = at least one does not.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NEEDLE = "tlc2.TLC"
FLAG = "-Djava.io.tmpdir"
SKIP_DIRS = {".git", ".check-logs", "build", "dist", "__pycache__", "node_modules"}


def shell_logical_lines(text):
    """Join backslash continuations so a multi-line `java ... \\` invocation is
    one unit -- otherwise the flag and tlc2.TLC land on different lines and the
    check is trivially fooled."""
    out, buf, start = [], "", 1
    for n, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = n
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        out.append((start, buf + stripped))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def py_arg_spans(text):
    """Yield (line_no, span_text) for each bracketed list containing tlc2.TLC.
    Walks out to the enclosing [...] so an argv list split over several lines is
    judged as one invocation."""
    for m in re.finditer(re.escape(NEEDLE), text):
        i = m.start()
        depth, lo = 0, None
        for j in range(i, -1, -1):
            if text[j] == "]":
                depth += 1
            elif text[j] == "[":
                if depth == 0:
                    lo = j
                    break
                depth -= 1
        depth, hi = 0, None
        for j in range(i, len(text)):
            if text[j] == "[":
                depth += 1
            elif text[j] == "]":
                if depth == 0:
                    hi = j
                    break
                depth -= 1
        if lo is None or hi is None:
            lo, hi = max(0, i - 400), min(len(text), i + 400)
        yield text.count("\n", 0, i) + 1, text[lo:hi + 1]


def main():
    bad = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith((".sh", ".py")):
                continue
            path = os.path.join(dirpath, fn)
            if os.path.abspath(path) == os.path.abspath(__file__):
                continue
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if NEEDLE not in text:
                continue
            scanned += 1
            rel = os.path.relpath(path, ROOT)
            if fn.endswith(".sh"):
                for lineno, logical in shell_logical_lines(text):
                    if NEEDLE in logical and FLAG not in logical:
                        bad.append((rel, lineno, logical.strip()[:120]))
            else:
                for lineno, span in py_arg_spans(text):
                    if FLAG not in span:
                        bad.append((rel, lineno, span.strip().replace("\n", " ")[:120]))

    if bad:
        print("TLC invocations missing %s (tlaplus/tlaplus#688):\n" % FLAG)
        for rel, lineno, snippet in bad:
            print("  %s:%d" % (rel, lineno))
            print("      %s" % snippet)
        print("\nEvery TLC JVM needs a PRIVATE java.io.tmpdir. Without it,")
        print("concurrent runs corrupt the standard modules SANY extracts from")
        print("the jar, and the resulting parse abort looks exactly like a")
        print("negative control that stopped detecting its bug.")
        print("See tools/verify/tla/README.md.")
        return 1

    print("OK: %d file(s) invoke TLC; every invocation carries %s" % (scanned, FLAG))
    return 0


if __name__ == "__main__":
    sys.exit(main())
