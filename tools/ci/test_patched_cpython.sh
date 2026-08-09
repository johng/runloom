#!/usr/bin/env bash
# test_patched_cpython.sh -- run BOTH suites against a patched interpreter built
# by build_patched_cpython.sh, and require BOTH to be fully green:
#
#   A. CPython's own stdlib suite  (python -m test)  -- must report SUCCESS
#   B. runloom's suite             (tests/run_isolated.py) -- validation only
#
# Suite A closes a gap the patches shipped with: src/patches/README.md records
# exec-home as "VALIDATED end-to-end ... **Not** run against the CPython test
# suite".  It is now run against it, and each released version is pinned to a
# release where the stdlib suite passes clean.
#
# NO EXPECTED-FAILURE LISTS.  The contract is simple: a released, patched
# interpreter passes all the tests.  If a pinned release does not, the fix is to
# pin a release that does (or, for a genuine UPSTREAM bug unrelated to the
# patches that no release fixes, exclude that one test in versions.env with a
# written reason -- see PY314_TEST_EXCLUDE).  There is deliberately no mechanism
# for carrying runloom-caused failures forward.
#
# Usage:  tools/ci/test_patched_cpython.sh <version> [--only=cpython|runloom]
# Env:    RL_CI_WORK, RL_CI_PREFIX (as build_patched_cpython.sh)
#         RL_CI_CPYTHON_TEST_ARGS  extra args for `python -m test`
#         RL_CI_TEST_TIMEOUT       per-test timeout, seconds (default 900)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
# shellcheck source=versions.env
. "$HERE/versions.env"

VERSION="${1:-}"
[ -n "$VERSION" ] || rl_die "usage: $0 <version> [--only=cpython|runloom]"
rl_validate_version "$VERSION"
shift

ONLY=all
for a in "$@"; do
    case "$a" in
        --only=*)  ONLY="${a#--only=}" ;;
        *)         rl_die "unknown argument: $a" ;;
    esac
done

WORK="${RL_CI_WORK:-$HOME/.cache/runloom-ci}"
PLATFORM="$(rl_platform)"
TIMEOUT="${RL_CI_TEST_TIMEOUT:-900}"
JOBS="$(rl_nproc)"

PYBIN="$(cat "$WORK/pybin-$VERSION-$PLATFORM.txt" 2>/dev/null || true)"
[ -x "${PYBIN:-}" ] || rl_die "no built interpreter for $VERSION on $PLATFORM -- run tools/ci/build_patched_cpython.sh $VERSION first"

# Free-threaded means free-threaded; a stray PYTHON_GIL=1 in the environment
# would silently re-enable the GIL and make the whole run meaningless.
export PYTHON_GIL=0
rc_total=0
soft_fail=0

# ---- A. CPython stdlib suite ------------------------------------------------

SERIES="$(rl_series_of_version "$VERSION")"
if [ "$ONLY" = all ] || [ "$ONLY" = cpython ]; then
    rl_step "CPython $VERSION stdlib suite (this takes a while)"

    # Per-series upstream-only exclusions (PY<series>_TEST_EXCLUDE in
    # versions.env).  These are documented UPSTREAM bugs that no pinned release
    # fixes -- never a place to hide a runloom-caused failure.
    EXCLUDE="$(rl_exclude_of_version "$VERSION")"
    EXCLUDE_ARGS=""
    for t in $EXCLUDE; do EXCLUDE_ARGS="$EXCLUDE_ARGS -x $t"; done
    [ -n "$EXCLUDE_ARGS" ] && rl_log "excluding (upstream-only, see versions.env):$EXCLUDE"

    # shellcheck disable=SC2086
    "$PYBIN" -m test \
        -j"$JOBS" \
        --timeout="$TIMEOUT" \
        $EXCLUDE_ARGS \
        ${RL_CI_CPYTHON_TEST_ARGS:-} \
        > "$WORK/cpython-tests-$VERSION.log" 2>&1
    regrtest_rc=$?

    # regrtest exits 0 iff every non-skipped test passed.  That IS the gate:
    # a released patched interpreter must pass the stdlib suite clean.
    okcount="$(grep -oE '[0-9]+ tests OK' "$WORK/cpython-tests-$VERSION.log" | tail -1)"
    if [ "$regrtest_rc" -eq 0 ]; then
        rl_log "CPython stdlib suite: SUCCESS ($okcount)"
        rl_ci_summary "✅ **CPython $VERSION stdlib** ($PLATFORM): SUCCESS — $okcount${EXCLUDE:+ · excluded: $EXCLUDE}"
    else
        rl_warn "CPython stdlib suite FAILED (regrtest exit=$regrtest_rc)"
        _failed="$(grep -E '^    test_' "$WORK/cpython-tests-$VERSION.log" | tr -s ' ' | paste -sd' ' -)"
        grep -E "tests failed:|^    test_|Result: FAILURE" "$WORK/cpython-tests-$VERSION.log" | tail -30 >&2
        rl_warn "full log: $WORK/cpython-tests-$VERSION.log"
        rl_warn "If this is an UPSTREAM bug no release fixes, exclude it in versions.env (PY${SERIES}_TEST_EXCLUDE) with a reason. Otherwise pin a release that passes."
        rl_ci_summary "❌ **CPython $VERSION stdlib** ($PLATFORM): FAILED (regrtest exit=$regrtest_rc) —$_failed"
        rc_total=1
    fi
fi

# ---- B. runloom suite -------------------------------------------------------

if [ "$ONLY" = all ] || [ "$ONLY" = runloom ]; then
    rl_step "build runloom against the patched interpreter"
    # pytest is REQUIRED -- without it run_isolated.py reports every one of the
    # ~240 files as failed, which reads like a catastrophic regression rather
    # than a missing dependency.
    "$PYBIN" -m pip install -q pytest \
        || rl_die "could not install pytest into the patched interpreter"
    # hypothesis is best-effort and MUST be installed separately: below 3.14 its
    # Rust/PyO3 core refuses to build against a free-threaded interpreter, and a
    # combined `pip install pytest hypothesis` fails the whole transaction --
    # taking pytest down with it.  Exactly one test file uses it.
    "$PYBIN" -m pip install -q hypothesis 2>/dev/null \
        || rl_warn "hypothesis unavailable on this interpreter (PyO3 has no free-threaded support below 3.14) -- the one test file that uses it will fail"
    ( cd "$ROOT" && "$PYBIN" setup.py build_ext --inplace ) > "$WORK/runloom-build-$VERSION.log" 2>&1 \
        || { tail -40 "$WORK/runloom-build-$VERSION.log" >&2; rl_die "runloom failed to build against the patched interpreter"; }

    # The end-to-end proof that the patches reached an EXTENSION MODULE, not just
    # CPython's own TUs.  If pyconfig.h had not been armed, these read 0 while
    # the interpreter itself still worked -- exactly the silent-mismatch case.
    rl_step "verify migration capability bits"
    ( cd "$ROOT" && PYTHONPATH=src "$PYBIN" - <<'PYEOF'
import sys, runloom_c
# src/runloom_c/ is the C SOURCE directory, so if the extension failed to build,
# `import runloom_c` silently succeeds as an implicit namespace package with no
# attributes -- which surfaces later as a baffling AttributeError deep in
# runtime.py rather than "the extension is missing".  Catch it here.
if getattr(runloom_c, "__file__", None) is None:
    sys.exit("FAIL: 'runloom_c' resolved to the src/runloom_c/ SOURCE directory as a "
             "namespace package -- the extension module was not built")
import runloom
status = runloom.migration_status()
print("migration_status():", status)
print("alloc_home_available:", runloom_c.alloc_home_available)
print("exec_home_available: ", runloom_c.exec_home_available)
missing = [k for k in ("alloc_home", "exec_home") if not status.get(k)]
if missing:
    sys.exit("FAIL: patched build does not advertise: %s -- the patch did not "
             "reach the extension module (check pyconfig.h)" % ", ".join(missing))
if not runloom.migration_available():
    sys.exit("FAIL: migration_available() is False on a fully patched build")
print("OK: both halves present, migration_available() is True")
PYEOF
    ) && rl_ci_summary "✅ **runloom migration** ($VERSION, $PLATFORM): built + migration_available()" \
      || { rc_total=1; rl_warn "capability check FAILED"
           rl_ci_summary "❌ **runloom migration** ($VERSION, $PLATFORM): capability check FAILED"; }

    # runloom's OWN suite is validation, not part of the CPython release gate.
    # It has failures that reproduce on STOCK CPython (test_mn_sim_bytes
    # late-parker, test_linz_battery -- see CLAUDE.md), so they measure runloom,
    # not the patched interpreter, and must not block a CPython release.  Run it,
    # report it loudly, but track it in a SEPARATE soft counter.
    rl_step "runloom suite (tests/run_isolated.py) -- validation, not a release gate"
    ( cd "$ROOT" && PYTHONPATH=src "$PYBIN" tests/run_isolated.py ) \
      && rl_ci_summary "✅ **runloom suite** ($VERSION, $PLATFORM): passed" \
      || { rl_warn "runloom suite has failures (see above) -- reported, does NOT gate the CPython release"
           rl_ci_summary "⚠️ **runloom suite** ($VERSION, $PLATFORM): failures (validation only, does not gate the release)"
           soft_fail=1; }

    # NOTE: scripts/check_all_fast.sh (runloom's local pre-merge gate: formal
    # verification via TLC, Spin/CBMC proofs, the M:N fuzzer, mn-stress) is
    # deliberately NOT run here.  It is a runloom DEVELOPMENT gate, not a
    # patched-CPython validation, and it is CI-hostile: heavy enough to OOM a
    # 2-core hosted runner and known to crash on 3.13t (the gh-116738 heapq
    # SIGSEGV, a stdlib bug fixed in 3.14t -- see CLAUDE.md).  A crash there
    # cannot be made reliably non-gating from inside a subshell (an OOM-kill
    # lands on the parent), so it stays out of this CI entirely.  Run it locally
    # against 3.14t (scripts/check_all_fast.sh) as part of runloom development.
fi

# The CPython RELEASE gate is rc_total: patches applied, interpreter built,
# stdlib suite clean, runloom builds and migration_available() is True.  The
# runloom suite is validation (soft_fail) -- reported, but a pre-existing runloom
# test failure (which reproduces on STOCK CPython) must not block shipping a
# correct patched interpreter.
if [ "$soft_fail" -ne 0 ]; then
    rl_warn "runloom validation had non-fatal failures (see above) -- NOT gating the release"
fi
if [ "$rc_total" -eq 0 ]; then
    rl_step "TESTS OK -- $VERSION on $PLATFORM (release gate green$([ "$soft_fail" -ne 0 ] && printf '; validation had warnings'))"
else
    rl_step "TESTS FAILED -- $VERSION on $PLATFORM (release gate)"
fi
exit "$rc_total"
