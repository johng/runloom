"""Import-time ABI guard for the runloom C extension.

WHY THIS EXISTS
    runloom's cross-hub migration needs a *patched* free-threaded CPython
    (Py_TSTATE_ALLOC_HOME + Py_TSTATE_EXEC_HOME, see src/patches/).  The
    alloc-home patch adds the `_alloc_home` field to `_PyThreadStateImpl`, so an
    extension compiled against the patched headers assumes a DIFFERENT struct
    layout than a stock free-threaded interpreter.  Loading a wheel built for
    one on the other "shifts struct offsets silently rather than failing to
    link" (src/patches/README.md) -- memory corruption, not an ImportError.

    The Python wheel-tag system cannot catch this: the patched and stock
    free-threaded interpreters BOTH report SOABI `cpython-3NNt`, so a wheel
    tagged `cp3NNt` (and the `.cpython-3NNt-<plat>.so` file inside it) matches
    both.  `packaging.tags` reconstructs the CPython ABI tag from
    Py_GIL_DISABLED / Py_DEBUG only -- it never reads the patch flags -- so no
    SOABI edit makes pip distinguish the two while `sys.implementation.name`
    stays "cpython".

    This guard closes the gap at import: it compares what the extension was
    COMPILED with (pure #ifdef witnesses on runloom_c) against what the RUNNING
    interpreter provides (sysconfig), and refuses to import on a layout mismatch
    with an actionable message instead of corrupting later.

SEVERITY
    hard (ImportError):
        * alloc-home layout differs  -> _PyThreadStateImpl offsets shift
        * free-threading differs      -> different object model entirely
        * CPython major.minor differs -> runloom uses internal headers
    soft (warning):
        * exec-home differs -- a codegen property, not a struct layout; only
          matters once migration is actually enabled, and migration also
          requires alloc-home (already hard-checked above).

    Set RUNLOOM_SKIP_ABI_CHECK=1 to bypass entirely (dev / debugging only).
"""

from __future__ import annotations

import os
import sys
import sysconfig
import warnings

__all__ = ["check_abi", "abi_report", "AbiMismatch"]


class AbiMismatch(ImportError):
    """The runloom extension was built for a different interpreter ABI."""


def _truthy(v) -> bool:
    # sysconfig values arrive as 1/0, "1"/"0", or None.
    if v is None:
        return False
    try:
        return int(v) != 0
    except (TypeError, ValueError):
        return bool(v)


def _minor(hexversion: int) -> tuple[int, int]:
    return (hexversion >> 24) & 0xFF, (hexversion >> 16) & 0xFF


def _interp_facts() -> dict:
    """What the RUNNING interpreter provides, ABI-wise."""
    ft = _truthy(sysconfig.get_config_var("Py_GIL_DISABLED"))
    return {
        "free_threaded": ft,
        # The patch fields only exist on a free-threaded build; couple the reads
        # so a GIL build never reads a stale flag as "has alloc-home".
        "alloc_home": ft and _truthy(sysconfig.get_config_var("Py_TSTATE_ALLOC_HOME")),
        "exec_home": ft and _truthy(sysconfig.get_config_var("Py_TSTATE_EXEC_HOME")),
        "py_minor": _minor(sys.hexversion),
    }


def _built_facts(core) -> dict | None:
    """What the extension (`core` == runloom_c) was COMPILED against.

    Returns None if the extension predates the build witnesses -- then the guard
    stays silent (nothing to compare against) rather than guessing.
    """
    if not hasattr(core, "built_alloc_home"):
        return None
    return {
        "free_threaded": bool(core.built_free_threaded),
        "alloc_home": bool(core.built_alloc_home),
        "exec_home": bool(core.built_exec_home),
        "py_minor": _minor(int(core.built_py_version_hex)),
    }


def abi_report(core) -> dict:
    """Structured comparison of the extension build vs the running interpreter.

    Keys: built, interp, hard (list of fatal mismatch names), soft (list of
    non-fatal mismatch names), skipped (bool).  Never raises.
    """
    built = _built_facts(core)
    interp = _interp_facts()
    if built is None:
        return {"built": None, "interp": interp, "hard": [], "soft": [],
                "skipped": True}

    hard, soft = [], []
    if built["py_minor"] != interp["py_minor"]:
        hard.append("cpython_version")
    if built["free_threaded"] != interp["free_threaded"]:
        hard.append("free_threading")
    if built["alloc_home"] != interp["alloc_home"]:
        hard.append("alloc_home")          # struct layout -> silent corruption
    if built["exec_home"] != interp["exec_home"]:
        soft.append("exec_home")           # codegen -> only bites under migration
    return {"built": built, "interp": interp, "hard": hard, "soft": soft,
            "skipped": False}


def _fmt(built: dict, interp: dict) -> str:
    def one(label, key, render=repr):
        return (f"    {label:<15} built={render(built[key]):<12} "
                f"interpreter={render(interp[key])}")
    ver = lambda mn: f"{mn[0]}.{mn[1]}"
    return "\n".join([
        one("cpython", "py_minor", ver),
        one("free_threaded", "free_threaded"),
        one("alloc_home", "alloc_home"),
        one("exec_home", "exec_home"),
    ])


def check_abi(core, *, raise_on_mismatch: bool = True) -> dict:
    """Compare and (by default) enforce the extension/interpreter ABI match.

    Called once at `import runloom`.  Raises AbiMismatch on a hard mismatch,
    warns on a soft one.  Returns the abi_report() dict for callers that want to
    inspect rather than enforce.  A no-op when RUNLOOM_SKIP_ABI_CHECK=1 or when
    the extension carries no build witnesses.
    """
    if os.environ.get("RUNLOOM_SKIP_ABI_CHECK") == "1":
        return {"skipped": True}

    report = abi_report(core)
    if report["skipped"]:
        return report

    built, interp = report["built"], report["interp"]

    if report["hard"] and raise_on_mismatch:
        raise AbiMismatch(
            "runloom's C extension was built for a different CPython ABI than "
            "the interpreter now importing it (" + ", ".join(report["hard"]) +
            " differ):\n" + _fmt(built, interp) + "\n"
            "This is the patched-vs-stock free-threaded hazard: both report the "
            "same SOABI/wheel tag, but the alloc-home patch changes the "
            "_PyThreadStateImpl layout, so using this extension here would "
            "corrupt memory.  Install the runloom build that matches this "
            "interpreter, or rebuild from source "
            "(`pip install --no-binary runloom runloom`).  Set "
            "RUNLOOM_SKIP_ABI_CHECK=1 to bypass (unsafe)."
        )

    if report["soft"]:
        warnings.warn(
            "runloom: exec-home build flag differs from this interpreter "
            f"(built={built['exec_home']}, interpreter={interp['exec_home']}). "
            "Harmless unless cross-hub migration is enabled; if you enable it, "
            "rebuild every extension against the patched headers "
            "(see src/patches/README.md).",
            RuntimeWarning, stacklevel=2,
        )
    return report
