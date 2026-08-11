#!/usr/bin/env bash
# classify_change.sh <base-ref> <head-ref>
#
# Classify a diff into the two CI concerns, printing two lines suitable for
# appending to $GITHUB_OUTPUT:
#
#   interp=true|false    an INTERPRETER-defining input changed -> build CPython
#                        from source + run the stdlib suite (the `build` job).
#   runloom=true|false   a RUNLOOM-side input changed -> validate the extension
#                        + suite against a released interpreter (the `validate`
#                        job, which downloads a prebuilt).
#
# These gate two INDEPENDENT jobs in one always-running workflow, so a single
# top-level `ci-success` job can aggregate them: whichever jobs are irrelevant to
# a change are skipped, and skipped != failed.  (A path-filtered *workflow* would
# instead post no check at all when skipped, which is why this lives in one
# workflow keyed on job-level booleans rather than two `on.paths` workflows.)
#
# .github/actions/** and the workflow file are in BOTH sets on purpose: editing a
# composite action or the workflow validates both source branches (build AND
# prebuilt).  A change relevant to neither set (docs) yields both=false -> both
# jobs skip -> ci-success is green with nothing built.
#
# FAIL SAFE: an unknown/all-zero/unresolvable base prints both=true (run
# everything).  fetch_prebuilt_cpython.sh independently re-checks a release's
# provenance against the current patch hashes, so a wrong `interp=false` still
# cannot test against a mismatched interpreter -- it falls back to a build.
set -uo pipefail

BASE="${1:-}"
HEAD="${2:-HEAD}"

both_true() { printf 'interp=true\nrunloom=true\n'; exit 0; }

[ -n "$BASE" ] || both_true
case "$BASE" in
    *[!0]*) : ;;          # a real ref/sha (contains a non-zero char)
    *)      both_true ;;  # empty or all-zero (new branch) -> run everything
esac
git rev-parse -q --verify "$BASE^{commit}" >/dev/null 2>&1 || both_true
git rev-parse -q --verify "$HEAD^{commit}" >/dev/null 2>&1 || both_true

MB="$(git merge-base "$BASE" "$HEAD" 2>/dev/null || printf '%s' "$BASE")"
FILES="$(git diff --name-only "$MB" "$HEAD" 2>/dev/null || true)"

interp=false
runloom=false
while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
        src/patches/*|\
        tools/ci/versions.env|\
        tools/ci/build_patched_cpython.sh|\
        tools/ci/lib.sh|\
        tools/ci/check_patches.sh|\
        tools/ci/package_release.sh|\
        tools/ci/classify_change.sh|\
        .github/actions/*|\
        .github/workflows/ci.yml)
            interp=true ;;
    esac
    case "$f" in
        src/runloom/*|\
        tests/*|\
        src/runloom_c/*|\
        setup.py|\
        tools/lincheck/*|\
        tools/ci/test_patched_cpython.sh|\
        tools/ci/fetch_prebuilt_cpython.sh|\
        tools/ci/classify_change.sh|\
        .github/actions/*|\
        .github/workflows/ci.yml)
            runloom=true ;;
    esac
done <<EOF
$FILES
EOF

printf 'interp=%s\nrunloom=%s\n' "$interp" "$runloom"
