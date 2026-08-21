# TLA+ specs — and one toolchain bug that looks like a broken spec

`run_tla.sh` model-checks every `Runloom*.tla` here. Each check comes in pairs:
a **correct** control that must hold, and a **negative** control that must be
*detected* — a deliberately injected bug that TLC is required to find. A
negative control that stops failing is a real regression, not a flake.

Which makes it important to know about the one failure mode that is neither.

## "Module-Table lookup failure" is not your spec

If TLC reports either of these:

```
Error: Parsing or semantic analysis failed. Module-Table lookup failure
       for module name X derived from X file name.
java.lang.NullPointerException: Cannot invoke "String.length()" because "str" is null

tla2sany.semantic.AbortException
```

then the named spec is almost certainly fine, and re-reading it will waste your
time. This is [tlaplus/tlaplus#688](https://github.com/tlaplus/tlaplus/issues/688),
"SANY fails randomly when run concurrently in several VMs".

### The mechanism

Our specs `EXTENDS Naturals`, `Sequences`, `FiniteSets`. Those modules ship
inside `tla2tools.jar`, and SANY cannot parse them from a jar entry — it has to
materialise them on disk first. `util.SimpleFilenameToStream.read()` does that
by extracting each one to

```
java.io.tmpdir + File.separator + "Naturals.tla"      →  /tmp/Naturals.tla
```

a **fixed, unqualified name** — no pid, no random suffix — written with a
truncating `FileOutputStream` and then marked `deleteOnExit()`.

So any two TLC JVMs sharing `/tmp` race on the same three files. One truncates
what the other is mid-parse, or exits and deletes it out from under a live
parse. The victim dies at parse time, before it ever reaches a verdict.

Two consequences that make this worse than an ordinary flake:

- **It is indiscriminate.** It strikes correct controls and negative controls
  alike, because it happens before TLC looks at the property at all.
- **It is silent.** `run_tla.sh` pipes each run into `grep -q`, so a parse abort
  and "the negative control stopped detecting its bug" collapse to the identical
  one-word `FAIL`. Those are opposite situations. `tlc_why` in `run_tla.sh`
  exists to tell them apart, and has a branch that names this cause explicitly.

### The fix, and why it lives in the runners

Upstream has already fixed it on `master`:

```java
static Path getTempDirectory() {
    return Files.createTempDirectory("tlc-");   // unique per process
}
```

but that is in **no released jar**. v1.7.4 (August 2024) is still the latest
release and still uses the shared fixed name, so pinning a newer jar is not an
option today.

Every TLC invocation in this repo therefore passes **`-Djava.io.tmpdir=<private
dir>`**, which achieves from outside exactly what master does internally. The
call sites are `run_tla.sh`, `hunt_tlc.sh`, and the six
`tools/*_trace_conform.py` helpers. `run_tla.sh` carries the full commentary.

**Do not remove that flag as tidying.** It is load-bearing, for two reasons.

The first is the flake above. Measured on this box: 24 concurrent TLC runs over
mixed specs produced 3 parse failures without it and 0 with it. In the lane, the
failure ran at roughly 1 in 40 `verify-fast` runs (9 in 380) — rare enough to
look like noise, common enough to erode trust in the gate.

The second is that the shared path is also a local security defect — CWE-377
(insecure temporary file) plus CWE-59 (symlink following). The name is
predictable and `FileOutputStream` follows symlinks, so any local user who can
write to `/tmp` can pre-create `/tmp/Naturals.tla` as a symlink and make TLC,
running as you, truncate and overwrite an arbitrary file you own. Demonstrated:

```
$ ln -s ~/victim.txt /tmp/Naturals.tla
$ java -cp tla2tools.jar tlc2.TLC -config RunloomWake.cfg RunloomWake.tla
$ head -1 ~/victim.txt
-------------------------------- MODULE Naturals ---------------------------
```

A private `java.io.tmpdir` closes that too, since the path stops being
predictable. That matters little on a single-user box and a great deal on a
shared one.

## If you hit it anyway

It means some TLC is running **without** the flag — a bare manual invocation, or
a new call site that missed it. `tlc_why` will say so. Add the flag rather than
retrying past the failure.
