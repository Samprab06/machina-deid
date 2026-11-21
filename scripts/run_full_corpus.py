"""Resumable parallel full-corpus runner. See docs/production-runner.md and docs/attempt-2.md.

    python scripts/run_full_corpus.py run --expected-revision <40-hex> [--root /mnt/machina-deid] [--workers 12]
"""
import sys
from pathlib import Path

# Always run the code of the checkout this script lives in (never an editable install of another attempt).
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

import machina_deid  # noqa: E402
from machina_deid.production import main  # noqa: E402

if Path(machina_deid.__file__).resolve().parents[1] != _SRC:
    raise SystemExit("machina_deid was not imported from this checkout")

if __name__ == "__main__":
    sys.exit(main(script=Path(__file__).resolve()))
