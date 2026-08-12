#!/usr/bin/env python3
"""Esecutore di test senza dipendenze.

Se `pytest` è installato lo usa; altrimenti attiva uno shim minimale
(`approx`, `parametrize`, `raises`, `tmp_path`) sufficiente per la suite di
questo progetto. Utile su macchine dove non si vuole/può installare nulla.

    python run_tests.py            # tutta la suite
    python run_tests.py golden     # solo i file che contengono "golden"
"""
from __future__ import annotations

import importlib.util
import inspect
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------- pytest shim
def _install_shim() -> None:
    m = types.ModuleType("pytest")

    class _Approx:
        def __init__(self, expected, rel=1e-6, abs_=1e-12):
            self.expected, self.rel, self.abs = expected, rel, abs_

        def __eq__(self, other):
            try:
                return abs(other - self.expected) <= max(
                    self.abs, self.rel * max(abs(self.expected), abs(other)))
            except TypeError:
                return NotImplemented

        def __repr__(self):
            return f"approx({self.expected})"

    def approx(expected, rel=1e-6, abs=1e-12):  # noqa: A002
        return _Approx(expected, rel, abs)

    class _Mark:
        @staticmethod
        def parametrize(argnames, argvalues):
            names = [a.strip() for a in argnames.split(",")]

            def deco(fn):
                fn._params = (names, list(argvalues))
                return fn
            return deco

        def __getattr__(self, _):
            def deco(fn=None, **_kw):
                return fn if fn is not None else (lambda f: f)
            return deco

    class _Raises:
        def __init__(self, exc):
            self.exc = exc

        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            if et is None:
                raise AssertionError(f"atteso {self.exc.__name__}, nessuna eccezione")
            return issubclass(et, self.exc)

    m.approx = approx
    m.mark = _Mark()
    m.raises = lambda exc: _Raises(exc)
    m.fixture = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
    m.skip = lambda reason="": (_ for _ in ()).throw(AssertionError("skip: " + reason))
    sys.modules["pytest"] = m


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(fn, name: str, tmp_root: Path) -> None:
    sig = inspect.signature(fn)
    if "tmp_path" in sig.parameters:
        d = Path(tempfile.mkdtemp(dir=tmp_root))
        fn(tmp_path=d)
    else:
        fn()


def main(argv: list[str]) -> int:
    try:
        import pytest  # noqa: F401
        native = True
    except ImportError:
        _install_shim()
        native = False

    if native:
        import pytest
        return pytest.main(["-q", str(ROOT / "tests"), *argv])

    print("pytest non installato → shim interno\n")
    pattern = argv[0] if argv else ""
    files = sorted(p for p in (ROOT / "tests").glob("test_*.py") if pattern in p.name)
    tmp_root = Path(tempfile.mkdtemp(prefix="mxfp4_tests_"))
    passed = failed = 0
    failures: list[tuple[str, str]] = []

    try:
        for f in files:
            mod = _load(f)
            print(f"── {f.name}")
            for name in sorted(dir(mod)):
                if not name.startswith("test_"):
                    continue
                fn = getattr(mod, name)
                if not callable(fn):
                    continue
                cases = [({}, "")]
                if hasattr(fn, "_params"):
                    names, values = fn._params
                    cases = [(dict(zip(names, v if isinstance(v, (tuple, list)) else (v,))),
                              f"[{v}]") for v in values]
                for kwargs, suffix in cases:
                    try:
                        if kwargs:
                            fn(**kwargs)
                        else:
                            _call(fn, name, tmp_root)
                        passed += 1
                        print(f"   ✔ {name}{suffix}")
                    except Exception:
                        failed += 1
                        failures.append((f"{f.name}::{name}{suffix}", traceback.format_exc()))
                        print(f"   ✘ {name}{suffix}")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    for name, tb in failures:
        print(f"\n{'=' * 70}\nFAIL {name}\n{tb}")
    print(f"\n{passed} passati, {failed} falliti")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
