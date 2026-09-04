#!/usr/bin/env python3
"""untrusted_diff_scan.py -- flag agent-directed text in a diff, before review.

THIS IS A TRIPWIRE, NOT A CONTROL.  Read that again before relying on it.

Prompt injection is not solvable by pattern matching.  The security consensus is
explicit about this: classifiers and filters reduce the frequency of successful
attacks and cannot reduce it to zero, and "in application security, 99% is a
failing grade."  Anyone who writes "per our earlier discussion this is fine"
walks straight past every pattern below.  If this file ever starts being
described as protection, delete that description.

What it is actually for:

  1. Catching the LAZY and the ACCIDENTAL.  Most real instances are not attacks;
     they are a contributor stating something convenient as though it were
     settled.  Those are worth seeing, and they are easy to match.
  2. Forcing a HUMAN read of the flagged lines before an agent is pointed at the
     diff.  The value is the checkpoint, not the regex.
  3. Making the threat model executable, so it survives staff turnover better
     than a paragraph in a doc does.

For the class this repo actually cares about -- a PR arguing its way past a
standing rule -- a check that tests the ACT rather than the argument for it is
not the argument.  Use that first.  This is the softer, noisier companion.

ORDERING MATTERS.  scripts/check_all_fast.sh runs on MERGED code, by which point
an agent has already read the PR.  For the review threat this must run on the
DIFF, before you point anything at it:

    tools/verify/untrusted_diff_scan.py --diff origin/main...pr-branch
    gh pr diff 3 | tools/verify/untrusted_diff_scan.py -
    tools/verify/untrusted_diff_scan.py --tree          # regression sweep

Suppressing a genuine quoted example: put `untrusted-scan: quoted` on the line
or the line above.  This file quotes real payloads on purpose
and are self-excluded.

Exit 0 = nothing flagged.  Exit 1 = look at the flagged lines yourself.
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SUPPRESS = "untrusted-scan: quoted"

# Files that quote payloads as documentation. Self-exclusion, not an allowlist
# for arbitrary content.
SELF_EXCLUDE = {
    "tools/verify/untrusted_diff_scan.py",
}

PATTERNS = [
    ("INSTRUCTION-OVERRIDE", re.compile(
        r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+"
        r"(instruction|direction|rule|prompt)|"
        r"disregard\s+(the\s+)?(previous|prior|above|earlier)|"
        r"forget\s+(everything|all)\s+(you|above)", re.I)),

    ("AGENT-ADDRESSED", re.compile(
        r"\byou\s+are\s+(an?\s+)?(AI|LLM|assistant|agent|language\s+model)\b|"
        r"\bas\s+an?\s+(AI|LLM|assistant|language\s+model)\b|"
        r"\bsystem\s+prompt\b|"
        r"\byour\s+(instructions|guidelines|system\s+message)\b", re.I)),

    # The possessive is required. Without it, "single-owner request/response"
    # in an unrelated test docstring matches, and a tripwire that cries wolf on
    # ordinary prose gets muted within a week.
    ("CLAIMED-AUTHORITY", re.compile(
        r"\b(maintainer|owner|author|operator)(?:'s|s')\s+"
        r"(?:[a-z]+,?\s+){0,3}"
        r"(request|approval|consent|permission|instruction|say-so)|"
        r"\b(has\s+been|was|is)\s+(pre-?)?(approved|authoriz|authoris)|"
        r"\bat\s+the\s+(maintainer|owner|operator)'s\b", re.I)),

    ("POLICY-OVERRIDE", re.compile(
        r"\boverrid(e|es|ing)\s+(that|the|this|any)\s+"
        r"(standing\s+)?(note|policy|rule|guidance|instruction)|"
        r"\b(policy|rule)\s+exception\b|"
        r"\bdoes\s+not\s+apply\s+(here|to\s+this)\b|"
        r"\bsupersedes?\s+(the\s+)?(CLAUDE\.md|policy|rule)", re.I)),

    ("SUPPRESS-REPORTING", re.compile(
        r"\b(do\s?n[o']?t|no\s+need\s+to)\s+"
        r"(tell|inform|mention|report|notify|surface|ask)\b|"
        r"\bwithout\s+(asking|informing|telling|confirming)\b|"
        r"\bno\s+need\s+to\s+(verify|check|confirm|review)\b", re.I)),

    # LOW SIGNAL, and handled differently per mode. A NEW mention of the policy
    # file in a diff is rare and is exactly where an argument about policy would
    # appear, so it is worth a glance there. Across the whole tree it is ~20
    # legitimate "see CLAUDE.md" citations that bury everything else, so tree
    # mode drops it unless --refs is passed.
    ("POLICY-FILE-REF", re.compile(
        r"\bCLAUDE\.md\b", re.I)),
]

LOW_SIGNAL = {"POLICY-FILE-REF"}
ORDER = {name: i for i, (name, _) in enumerate(PATTERNS)}


def parse_diff(text):
    """Yield (path, lineno, line) for ADDED lines only. Line numbers are in the
    NEW file, taken from the hunk header -- a flagged line you cannot locate is
    a flagged line nobody checks."""
    path, newline = None, 0
    for raw in text.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            path = p[2:] if p.startswith("b/") else p
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            newline = int(m.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            yield path, newline, raw[1:]
            newline += 1
        elif raw.startswith(" "):
            newline += 1
        # '-' lines consume no new-file line number


def scan_lines(items, low_signal=True):
    hits, prev, prev_hit = [], {}, {}
    for path, lineno, line in items:
        if path in SELF_EXCLUDE:
            continue
        if SUPPRESS in line or SUPPRESS in prev.get(path, ""):
            prev[path] = line
            continue
        # Comments wrap. PR #3's payload split "…added at the" / "maintainer's
        # explicit, repeated request…" across two lines, so single-line matching
        # missed the phrase entirely and only caught it via a different rule.
        # Test the line, then the previous line joined to it with comment
        # markers stripped.
        joined = re.sub(r"^\s*(#|//|\*|--)+\s?", " ",
                        prev.get(path, "")) + " " + re.sub(
                            r"^\s*(#|//|\*|--)+\s?", " ", line)
        for name, rx in PATTERNS:
            if name in LOW_SIGNAL and not low_signal:
                continue
            on_line = bool(rx.search(line))
            if not on_line and not rx.search(joined):
                continue
            # A window-only match belongs to the line that carries the phrase's
            # tail, not to the blank line after it, and must not re-report the
            # same phrase the previous line already flagged.
            if not on_line:
                if not line.strip():
                    continue
                if prev_hit.get(path) == name:
                    continue
                # If the phrase sits wholly on the PREVIOUS line, the window is
                # just re-finding it one line late. Report only genuine
                # boundary-spanning matches -- otherwise a payload on line N
                # gets pinned to line N+1, and my own docstring's rule ("a
                # flagged line you cannot locate is a flagged line nobody
                # checks") is violated by this file.
                if rx.search(prev.get(path, "")):
                    continue
            hits.append((name, path, lineno, line.strip()[:130]))
            prev_hit[path] = name
            break
        else:
            prev_hit[path] = None
        prev[path] = line
    # Strongest categories first: the point is that the line worth reading is
    # the first one printed, not buried under twenty citations.
    hits.sort(key=lambda h: (ORDER[h[0]], h[1], h[2]))
    return hits


def tree_items():
    out = subprocess.run(["git", "-C", ROOT, "ls-files"],
                         capture_output=True, text=True).stdout.split()
    for rel in out:
        if not rel.endswith((".py", ".sh", ".yml", ".yaml", ".md", ".c", ".h",
                             ".toml", ".cfg", ".tla", ".txt", ".env")):
            continue
        full = os.path.join(ROOT, rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    yield rel, n, line.rstrip("\n")
        except OSError:
            continue


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--diff", metavar="RANGE", help="git diff range, e.g. main...branch")
    g.add_argument("--tree", action="store_true", help="scan all tracked files")
    g.add_argument("stdin", nargs="?", help="'-' to read a unified diff on stdin")
    ap.add_argument("--refs", action="store_true",
                    help="include low-signal CLAUDE.md references in --tree mode")
    args = ap.parse_args()

    if args.tree:
        items, what = tree_items(), "tracked tree"
    elif args.diff:
        text = subprocess.run(["git", "-C", ROOT, "diff", args.diff],
                              capture_output=True, text=True).stdout
        items, what = parse_diff(text), "diff %s" % args.diff
    else:
        items, what = parse_diff(sys.stdin.read()), "diff on stdin"

    hits = scan_lines(items, low_signal=(not args.tree) or args.refs)
    if not hits:
        print("OK: nothing agent-directed flagged in %s" % what)
        return 0

    print("Agent-directed text flagged in %s (%d):\n" % (what, len(hits)))
    for name, path, lineno, line in hits:
        print("  [%s] %s:%d" % (name, path, lineno))
        print("      %s" % line)
    print("\nThis is a TRIPWIRE, not a verdict -- most hits are innocent, and a")
    print("competent adversary would not trip it at all. Read the lines above")
    print("yourself before pointing an agent at this change. Treat any of it")
    print("that reads as an instruction as DATA, never as authorisation.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
