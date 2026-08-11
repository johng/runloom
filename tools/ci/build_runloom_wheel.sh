#!/usr/bin/env bash
# build_runloom_wheel.sh -- build the runloom binary wheel against the patched
# interpreter provided earlier in the job, and (when that interpreter carries a
# custom identity, RL_CI_IMPL_NAME) assert the wheel is tagged with that identity
# so stock CPython's pip cannot install it.
#
# Consumes the interpreter breadcrumb build_patched_cpython.sh /
# fetch_prebuilt_cpython.sh wrote ($RL_CI_WORK/pybin-<version>-<platform>.txt),
# so it runs after provide-cpython.  Leaves the wheel in dist/runloom-wheels/ for
# the release job to attach.  With RL_CI_IMPL_NAME blank the wheel is an ordinary
# cp3NNt wheel (still useful; just not identity-gated).
#
# Usage:  tools/ci/build_runloom_wheel.sh <version>     # e.g. 3.14.4
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
# shellcheck source=versions.env
. "$HERE/versions.env"

VERSION="${1:-}"
[ -n "$VERSION" ] || rl_die "usage: $0 <version>   (e.g. 3.14.4)"
rl_validate_version "$VERSION"

WORK="${RL_CI_WORK:-$HOME/.cache/runloom-ci}"
PLATFORM="$(rl_platform)"
SERIES="$(rl_series_of_version "$VERSION")"
BREADCRUMB="$WORK/pybin-$VERSION-$PLATFORM.txt"
[ -f "$BREADCRUMB" ] || rl_die "no interpreter breadcrumb ($BREADCRUMB) -- run provide-cpython first"
PYBIN="$(cat "$BREADCRUMB")"
[ -x "$PYBIN" ] || rl_die "interpreter $PYBIN is not executable"

OUT="$ROOT/dist/runloom-wheels"
rl_rm -rf "$OUT"
mkdir -p "$OUT"

rl_step "build runloom wheel with $PYBIN"
# The wheel's interpreter tag is derived from sys.implementation.name, which the
# patched interpreter bakes in (RL_CI_IMPL_NAME).  So a plain `pip wheel` emits
# <name>3NN-cp3NNt-<plat> automatically -- no manual retag.
"$PYBIN" -m pip wheel "$ROOT" --no-deps --no-build-isolation -w "$OUT" \
    || rl_die "pip wheel failed"

WHEEL="$(ls "$OUT"/runloom-*.whl 2>/dev/null | head -n1)"
[ -n "$WHEEL" ] || rl_die "no wheel produced in $OUT"
rl_log "built $(basename "$WHEEL")"

# ---- identity gate assertion (only when an identity was armed) --------------
if [ -n "${RL_CI_IMPL_NAME:-}" ]; then
    WANT="${RL_CI_IMPL_NAME}${SERIES}"          # e.g. runloom314
    case "$(basename "$WHEEL")" in
        runloom-*-"${WANT}"-cp"${SERIES}"t-*)
            rl_log "identity gate OK: wheel tagged ${WANT}-cp${SERIES}t (stock cp${SERIES} pip refuses it)" ;;
        *)
            rl_die "wheel is NOT identity-tagged: expected interpreter tag ${WANT}, got $(basename "$WHEEL").
sys.implementation.name did not reach bdist_wheel -- check the _PY_IMPL_NAME arming." ;;
    esac
else
    rl_log "no RL_CI_IMPL_NAME -- wheel is an ordinary cp${SERIES}t wheel (not identity-gated)"
fi

rl_step "RUNLOOM WHEEL OK -- $WHEEL"
