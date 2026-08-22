# fetch_pinned.sh -- sourced helper: never run a download we cannot verify.
#
# WHY.  The verification tooling fetched two executable JARs (tla2tools ~9 MB,
# alloy ~21 MB) over HTTPS and then RAN them, with no integrity check at all.
# Both are .gitignored, so they are re-fetched on every fresh clone: the URL was
# the entire trust boundary for code that then vouches for the scheduler's
# correctness. The CPython build scripts had the same shape.
#
# TLS proves you talked to the host you resolved. It does not prove the host is
# uncompromised, that the release asset was not replaced, or that a proxy in the
# middle is honest. A pinned sha256 does, for the file's contents.
#
# No antivirus helps here, which is worth stating because it is a natural thing
# to reach for: a backdoored tla2tools.jar is a perfectly well-formed Java
# archive and matches no malware signature. Content pinning is the control.
#
# PROVENANCE OF THE PINS -- these are not all equally strong, and pretending
# otherwise would be the whole problem in miniature:
#
#   CPython   STRONG. python.org publishes a sigstore bundle beside each
#             tarball. Each pin below was checked against the digest inside
#             that bundle, and the embedded signing certificate names the
#             release manager for that series (3.14.4 -> hugo@python.org,
#             3.13.13 -> thomas@python.org, both via GitHub OAuth).
#             CAVEAT, stated plainly: the digest and the certificate identity
#             were checked; the Fulcio chain and the Rekor transparency-log
#             inclusion proof were NOT (that needs sigstore-python, which will
#             not build here). So: strong provenance, not a complete sigstore
#             verification.
#
#   JARs      TRUST-ON-FIRST-USE. Neither tlaplus/tlaplus nor AlloyTools
#             publishes a checksum or signature with its release assets (the
#             asset lists were checked). The pins are the bytes the canonical
#             GitHub release URL served, recorded on 2026-08-22, and confirmed
#             to match the copies already on this box. That detects tampering
#             from today forward; it cannot prove the artifact was genuine
#             before that. If either project ever ships signatures, upgrade
#             this to verify them.
#
# Usage:
#     . "$ROOT/tools/fetch_pinned.sh"
#     rl_fetch_pinned "$URL" "$SHA256" "$DEST" || <handle>
#
# Return codes are deliberately distinct, because "no network" and "the bytes
# are wrong" are opposite situations and must not both degrade to a skip:
#     0  file present and verified
#     1  download failed (offline, 404) -- callers may SKIP the phase
#     2  INTEGRITY FAILURE -- never skip past this, never run the artifact

rl_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1      # macOS
    fi
}

rl_pin_mismatch() {   # <file> <want> <got>
    echo "" >&2
    echo "  ****** INTEGRITY FAILURE ******" >&2
    echo "  file:     $1" >&2
    echo "  expected: $2" >&2
    echo "  actual:   $3" >&2
    echo "" >&2
    echo "  This artifact is NOT what it was pinned to, and it is about to be" >&2
    echo "  EXECUTED. Do not delete it and retry; do not paste the new hash in" >&2
    echo "  to make this stop. Either upstream re-cut the release (check the" >&2
    echo "  project's release page and the version in the URL), or something" >&2
    echo "  between you and it is not honest." >&2
    echo "" >&2
}

# rl_verify_pinned <file> <sha256>   -> 0 ok, 2 mismatch
rl_verify_pinned() {
    _got="$(rl_sha256 "$1")"
    [ "$_got" = "$2" ] && return 0
    rl_pin_mismatch "$1" "$2" "$_got"
    return 2
}

# rl_fetch_pinned <url> <sha256> <dest>
rl_fetch_pinned() {
    _u="$1"; _s="$2"; _d="$3"
    # An artifact already on disk is verified too. It is .gitignored and was
    # fetched by some earlier run under rules we cannot see from here.
    if [ -f "$_d" ]; then
        rl_verify_pinned "$_d" "$_s" || return 2
        return 0
    fi
    _t="$_d.part.$$"
    if ! curl -fsSL -o "$_t" "$_u" 2>/dev/null; then
        rm -f "$_t"
        return 1
    fi
    _got="$(rl_sha256 "$_t")"
    if [ "$_got" != "$_s" ]; then
        rl_pin_mismatch "$_u" "$_s" "$_got"
        # Keep the bad bytes OUT of the path the tool will execute, but do not
        # silently destroy evidence of a possible attack.
        mv "$_t" "$_d.REJECTED" 2>/dev/null
        echo "  rejected download kept at: $_d.REJECTED" >&2
        return 2
    fi
    mv "$_t" "$_d"
    return 0
}
