"""Read-only ZIP residual diagnostic over the frozen catalog (redacted counts only).

    python scripts/attempt2_zip_diagnostic.py --root ROOT --scratch /tmp/a2-zipdiag --limit 30

See ``machina_deid.zip_diagnostic`` for metric semantics and guarantees.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from machina_deid.zip_diagnostic import main  # noqa: E402

raise SystemExit(main())
