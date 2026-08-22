#!/usr/bin/env python3
"""download_pin_lint.py -- no tooling download may bypass rl_fetch_pinned.

WHY A REPO-WIDE LINT AND NOT PER-SITE TESTS.  The property worth protecting is
"every download in this repo is verified" -- a statement about ALL sites at
once. Pinning was originally landed by editing six files and checking each by
hand, which is testing INSTANCES of the property instead of the property. Two
things went wrong immediately, and both are exactly what this catches:

  1. A `git checkout` meant to undo a deliberately-corrupted test pin reverted
     the WHOLE file and silently removed the pinning edit from run_alloy.sh.
     Every per-file test still passed afterwards, because an unpinned script
     runs the jar perfectly well -- absence of a pin is not a test failure.
     Only re-reading the diff found it.
  2. The hand grep used to "verify" the six sites filtered on `http` appearing
     on the same line, so it missed tools/mutate/schemata/setup_dredd.sh, which
     builds its URL in a variable. A seventh unpinned download had been sitting
     there the whole time.

A green test after a restore proves nothing about what the restore removed.
An invariant that only exists in someone's head is one revert from gone. This
turns both into a one-second failure.

WHAT IT ENFORCES: no curl/wget/urlopen/urlretrieve/requests.get anywhere under
tools/ or scripts/, except inside fetch_pinned.sh itself. Everything else must
go through rl_fetch_pinned, which verifies a sha256 before the bytes are used.

Legitimate exceptions (a doc example, a localhost fetch in a bench harness) take
`download-pin-lint: allow` on the line, with a reason. Explicit and greppable
beats a pattern nobody remembers writing.

Exit 0 = every download is pinned.  Exit 1 = at least one bypasses the helper.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SUPPRESS = "download-pin-lint: allow"
HELPER = "tools/fetch_pinned.sh"

SCAN_DIRS = ("tools", "scripts")
FETCHERS = re.compile(
    r"(?:^|[;&|(`$\s])(curl|wget)\b"
    r"|\burllib\.request\.(urlopen|urlretrieve)\b"
    r"|\brequests\.(get|post)\b"
    r"|\bhttpx\.(get|post)\b"
)

# Files that legitimately contain the word but perform no fetch: this lint, and
# the helper that is the sanctioned way to do it.
SELF_EXCLUDE = {HELPER, "tools/verify/download_pin_lint.py"}


def tracked_files():
    out = subprocess.run(["git", "-C", ROOT, "ls-files"] + list(SCAN_DIRS),
                         capture_output=True, text=True).stdout.split()
    return [p for p in out if p.endswith((".sh", ".py"))]


def main():
    bad = []
    for rel in tracked_files():
        if rel in SELF_EXCLUDE:
            continue
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        pinned_here = any("rl_fetch_pinned" in ln for ln in lines)
        for n, line in enumerate(lines, 1):
            if SUPPRESS in line:
                continue
            stripped = line.strip()
            # A comment mentioning curl is not a download. Shell and Python
            # both use '#'; this is deliberately crude and that is fine --
            # a real invocation is not indented behind a '#'.
            if stripped.startswith("#"):
                continue
            if not FETCHERS.search(line):
                continue
            bad.append((rel, n, stripped[:110], pinned_here))

    if bad:
        print("Unpinned download(s) -- these bypass rl_fetch_pinned:\n")
        for rel, n, snippet, pinned_here in bad:
            print("  %s:%d" % (rel, n))
            print("      %s" % snippet)
            if pinned_here:
                print("      NOTE: this file DOES call rl_fetch_pinned elsewhere --")
                print("            likely a second fetch that was missed.")
        print("""
Route it through the helper so the bytes are verified before use:

    . "$ROOT/tools/fetch_pinned.sh"
    rl_fetch_pinned "$URL" "$SHA256" "$DEST"
    case $? in
        0) : ;;                                  # verified
        1) echo "offline -- skipping"; exit 0 ;;  # download failed
        2) echo "FAILED its pin"; exit 1 ;;       # INTEGRITY FAILURE
    esac

Take the sha256 from upstream's signature or checksum if it publishes one, and
record in tools/fetch_pinned.sh which artifacts are properly verified and which
are only trust-on-first-use. If the line genuinely does not fetch anything
remote, mark it `download-pin-lint: allow` with a reason.""")
        return 1

    n = len(tracked_files())
    print("OK: %d tooling file(s) scanned; every download goes through rl_fetch_pinned" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
