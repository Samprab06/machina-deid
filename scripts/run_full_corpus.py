"""Resumable parallel full-corpus runner. See docs/production-runner.md.

    python scripts/run_full_corpus.py run --expected-revision <40-hex> [--root /mnt/machina-deid] [--workers 12]
"""
import sys
from pathlib import Path

from machina_deid.production import main

if __name__ == "__main__":
    sys.exit(main(script=Path(__file__).resolve()))
