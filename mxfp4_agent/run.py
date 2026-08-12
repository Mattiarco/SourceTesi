#!/usr/bin/env python3
"""Entry point diretto: `python run.py "la mia richiesta"`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mxfp4agent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
