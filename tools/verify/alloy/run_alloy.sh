#!/usr/bin/env bash
# run_alloy.sh -- check the netpoll-parker structural invariant with Alloy.
#
# Formalizes runloom_self_check's relational invariant (verify/alloy/selfcheck.als)
# and asserts two commands, mirroring the verify/ negative-control style:
#   WellFormedImpliesOK  -> expect UNSAT (valid: runtime well-formedness
#                           implies the self_check invariant)
#   BucketsAlwaysOnGlobal -> expect SAT  (counterexample: a dangling bucket
#                           entry not on the global list -- the exact bug
#                           self_check's "bucket entries not in global list"
#                           detects at runtime)
# Prints "N passed, M failed" so run_verify.sh can fold it into the total.
# Needs java; fetches the Alloy jar on first run (cached next to this script).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
JAR="${ALLOY_JAR:-$HERE/alloy.jar}"
URL="https://github.com/AlloyTools/org.alloytools.alloy/releases/download/v6.2.0/org.alloytools.alloy.dist.jar"
JAR_SHA256="6b8c1cb5bc93bedfc7c61435c4e1ab6e688a242dc702a394628d9a9801edb78d"

echo "-- Alloy (structural invariant of the netpoll parker graph) --"
if ! command -v java >/dev/null 2>&1; then
    echo "  (java not found -- skipping Alloy)"; exit 0
fi
# .gitignored and then executed -- pin it. Trust-on-first-use; AlloyTools
# publishes no checksums. See tools/fetch_pinned.sh.
. "$(cd "$HERE/../../.." && pwd)/tools/fetch_pinned.sh"
rl_fetch_pinned "$URL" "$JAR_SHA256" "$JAR"
case $? in
    0) : ;;
    1) echo "  (could not fetch Alloy jar -- skipping)"; exit 0 ;;
    2) echo "  alloy.jar FAILED its pin -- refusing to run it."; exit 1 ;;
esac

RM="$(command -v safe-rm || echo rm)"
PY="${PYTHON:-python3}"
$RM -rf "$HERE/selfcheck" 2>/dev/null
( cd "$HERE" && java -jar "$JAR" exec selfcheck.als >/dev/null 2>&1 )

# receipt.json is authoritative: a `check` command is SAT iff it produced a
# solution instance (a counterexample); otherwise UNSAT (valid).
verdicts="$("$PY" - "$HERE/selfcheck/receipt.json" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception as exc:
    print("ERROR", exc); sys.exit(0)
for name, c in r.get("commands", {}).items():
    sol = c.get("solution")
    sat = bool(sol) and any(s.get("instances") for s in sol)
    print("{0} {1}".format(name, "SAT" if sat else "UNSAT"))
PY
)"

pass=0; fail=0
expect() {  # command-name, expected-verdict (UNSAT|SAT), description
    local name="$1" want="$2" desc="$3"
    local got; got="$(echo "$verdicts" | awk -v n="$name" '$1==n{print $2}')"
    printf '  [alloy] %-24s ' "$name"
    if [ "$got" = "$want" ]; then
        echo "PASS -- $desc"; pass=$((pass+1))
    else
        echo "FAIL (wanted $want, got ${got:-<none>}) -- $desc"; fail=$((fail+1))
    fi
}
expect WellFormedImpliesOK  UNSAT "runtime well-formedness implies the self_check invariant (valid)"
expect BucketsAlwaysOnGlobal SAT  "correctly DETECTS a dangling bucket entry (bucket not on global list)"

$RM -rf "$HERE/selfcheck" 2>/dev/null
echo "  $pass passed, $fail failed"
[ "$fail" -eq 0 ]
