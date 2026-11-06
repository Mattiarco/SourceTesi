"""Test della CLI e dell'I/O di processo."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_utf8_output_survives_a_legacy_codepage():
    # Simula Windows con stdout rediretto: senza force_utf8_stdio il primo
    # carattere non-cp1252 fa crollare il processo.
    code = ("import sys; sys.path.insert(0, r'%s'); "
            "from mxfp4agent.utils import force_utf8_stdio; force_utf8_stdio(); "
            "print('ok → ✔ ⚠️')" % ROOT)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert "✔" in p.stdout.decode("utf-8")


def test_parser_offers_only_local_providers():
    sys.path.insert(0, str(ROOT))
    from mxfp4agent.cli import build_parser

    p = build_parser()
    action = next(a for a in p._actions if a.dest == "provider")
    assert set(action.choices) == {"ollama", "mock"}
    assert not any(a.dest == "api_key" for a in p._actions)
