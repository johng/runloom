"""Import-time ABI guard (runloom/_abi.py) -- the patched-vs-stock safety net.

runloom's migration build needs a patched free-threaded CPython whose alloc-home
patch changes the _PyThreadStateImpl layout.  That build and a stock 3.NNt build
share the SAME wheel tag / SOABI (cpython-3NNt), so pip and the import machinery
cannot tell a prebuilt runloom wheel built for one from the other -- loading the
wrong one shifts struct offsets silently (memory corruption, not ImportError).

The guard compares pure #ifdef build witnesses exposed by runloom_c
(built_alloc_home / built_exec_home / built_free_threaded / built_py_version_hex)
against the running interpreter's sysconfig flags, and refuses to import on a
layout mismatch.  These tests pin:

  * the real extension passes on the interpreter it was built for (no false
    positive -- this is the one that would break every normal run if the guard
    were wrong);
  * every hard mismatch (alloc-home layout, free-threading, cpython minor) is
    caught, and exec-home-only differences stay a soft warning;
  * the env bypass and the missing-witness fallback behave.
"""
import sys
import sysconfig
import types
import warnings

import pytest

import runloom_c
from runloom import _abi


HEX_314 = 0x030E04F0  # 3.14.4
HEX_313 = 0x030D0DF0  # 3.13.13


def _fake_core(ft, alloc, exec_, hexv=HEX_314):
    return types.SimpleNamespace(
        built_free_threaded=ft, built_alloc_home=alloc,
        built_exec_home=exec_, built_py_version_hex=hexv)


def _fake_interp(monkeypatch, ft, alloc, exec_):
    real = sysconfig.get_config_var
    vals = {"Py_GIL_DISABLED": ft, "Py_TSTATE_ALLOC_HOME": alloc,
            "Py_TSTATE_EXEC_HOME": exec_}
    monkeypatch.setattr(sysconfig, "get_config_var",
                        lambda name: vals.get(name, real(name)))


# --- the invariant that matters most: no false positive on the real build ----

def test_real_extension_matches_this_interpreter():
    """check_abi() on the extension we imported must NOT raise on the very
    interpreter it was compiled against -- else `import runloom` is broken."""
    report = _abi.check_abi(runloom_c)
    assert report.get("hard", []) == [], report
    # And the witnesses are actually present (not the silent-skip path).
    assert hasattr(runloom_c, "built_alloc_home")
    assert not report.get("skipped")


# --- hard mismatches must raise -----------------------------------------------

@pytest.mark.parametrize("built,interp,reason", [
    # patched wheel (alloc-home) loaded on a stock interpreter -> the hazard
    (_fake_core(1, 1, 1), (1, 0, 0), "alloc_home"),
    # stock wheel loaded on a patched interpreter -> reverse layout skew
    (_fake_core(1, 0, 0), (1, 1, 1), "alloc_home"),
    # free-threading mismatch
    (_fake_core(0, 0, 0), (1, 0, 0), "free_threading"),
    (_fake_core(1, 0, 0), (0, 0, 0), "free_threading"),
])
def test_hard_mismatch_raises(monkeypatch, built, interp, reason):
    _fake_interp(monkeypatch, *interp)
    with pytest.raises(_abi.AbiMismatch) as ei:
        _abi.check_abi(built)
    assert reason in _abi.abi_report(built)["hard"]
    # the message names the mismatch and points at a fix
    assert "corrupt memory" in str(ei.value)


def test_cpython_minor_mismatch_raises(monkeypatch):
    _fake_interp(monkeypatch, 1, 1, 1)          # interp is this 3.14 build
    built = _fake_core(1, 1, 1, hexv=HEX_313)   # wheel says it was built for 3.13
    with pytest.raises(_abi.AbiMismatch):
        _abi.check_abi(built)
    assert "cpython_version" in _abi.abi_report(built)["hard"]


# --- exec-home-only is a soft warning, not a hard failure ---------------------

def test_exec_home_only_is_soft(monkeypatch):
    _fake_interp(monkeypatch, 1, 1, 1)          # interp has both halves
    built = _fake_core(1, 1, 0)                 # wheel built without exec-home
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        rep = _abi.check_abi(built)             # must NOT raise
    assert rep["hard"] == [] and rep["soft"] == ["exec_home"]
    assert any(issubclass(x.category, RuntimeWarning) for x in w)


# --- matching builds never raise ----------------------------------------------

@pytest.mark.parametrize("ft,alloc,exec_", [(1, 1, 1), (1, 0, 0), (0, 0, 0)])
def test_matching_build_ok(monkeypatch, ft, alloc, exec_):
    _fake_interp(monkeypatch, ft, alloc, exec_)
    rep = _abi.check_abi(_fake_core(ft, alloc, exec_))
    assert rep["hard"] == [] and rep["soft"] == []


# --- escape hatches -----------------------------------------------------------

def test_env_bypass(monkeypatch):
    _fake_interp(monkeypatch, 1, 0, 0)                       # stock interp
    monkeypatch.setenv("RUNLOOM_SKIP_ABI_CHECK", "1")
    # patched wheel on stock interp would normally raise; bypass makes it a no-op
    assert _abi.check_abi(_fake_core(1, 1, 1)) == {"skipped": True}


def test_missing_witnesses_skips(monkeypatch):
    _fake_interp(monkeypatch, 1, 0, 0)
    rep = _abi.check_abi(types.SimpleNamespace())   # older ext, no witnesses
    assert rep["skipped"] is True and rep["hard"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
