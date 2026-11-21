"""Read-only residual diagnostic / local pilot over the frozen catalog (counts only).

    python scripts/attempt2_residual_pilot.py --root ROOT --scratch /tmp/a2-pilot --per-ext 100

See ``machina_deid.residual_pilot`` for guarantees.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from machina_deid.residual_pilot import main  # noqa: E402

raise SystemExit(main())
