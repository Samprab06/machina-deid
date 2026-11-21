"""Measured synthetic throughput of the production runner (serial vs parallel). Synthetic data only.

    python scripts/production_throughput.py --groups 40 --workers 1 2 [--workspace /tmp/machina-bench]
"""
import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from production_fixtures import file_set, make_workspace, tree_digest  # noqa: E402

from machina_deid import production as pr  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=40, help="6 files per group")
    ap.add_argument("--workers", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--workspace", type=Path, default=None)
    a = ap.parse_args()
    base = a.workspace or Path(tempfile.mkdtemp(prefix="machina-bench-"))
    files = file_set(a.groups)
    results, digests = [], []
    for w in a.workers:
        root = base / f"w{w}"
        if root.exists():
            shutil.rmtree(root)
        head = make_workspace(root, files)
        cfg = pr.Config(root=root, expected_rows=len(files), expected_revision=head, workers=w, reserve_bytes=0,
                        poll_seconds=0.05)
        t0 = time.perf_counter()
        pr.run(cfg, discovery_only=True)
        t1 = time.perf_counter()
        out = pr.run(cfg, package_output=False)
        t2 = time.perf_counter()
        assert out["remaining"] == 0
        digests.append(tree_digest(root / "attempts/attempt-1/corpus"))
        results.append({"workers": w, "files": len(files), "discovery_s": round(t1 - t0, 2),
                        "transform_s": round(t2 - t1, 2), "files_per_s": round(len(files) / (t2 - t0), 1)})
    for r in results:
        r["speedup_vs_first"] = round((results[0]["discovery_s"] + results[0]["transform_s"]) /
                                      (r["discovery_s"] + r["transform_s"]), 2)
    print(json.dumps({"results": results, "outputs_identical_modulo_dxf_save_metadata":
                      all(d == digests[0] for d in digests)}, indent=2))
    if a.workspace is None:
        shutil.rmtree(base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
