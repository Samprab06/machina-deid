"""Reproducibility check: run the synthetic scope workspace twice and list outputs whose bytes differ.

uv run python scripts/repro_check.py      (synthetic data only; prints relative paths of differing outputs)
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from production_fixtures import make_workspace  # noqa: E402
from test_scope import cfg, corpus  # noqa: E402

from machina_deid import production as pr  # noqa: E402


def once(files: dict) -> dict:
    t = Path(tempfile.mkdtemp(prefix="repro-"))
    head = make_workspace(t, files)
    pr.run(cfg(t, head, len(files)))
    c = t / "attempts/attempt-1/corpus"
    return {p.relative_to(c).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in c.rglob("*") if p.is_file()}


if __name__ == "__main__":
    files = corpus(Path(tempfile.mkdtemp(prefix="repro-gen-")))
    a, b = once(files), once(files)
    diff = sorted(k for k in a if a[k] != b.get(k))
    print(json.dumps({"outputs": len(a), "differ": diff}))
    sys.exit(1 if diff else 0)
