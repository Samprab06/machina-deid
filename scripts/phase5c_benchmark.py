#!/usr/bin/env python3
"""Bounded real Phase 5C benchmark: discovery pass -> catalog auto-approval -> transformation pass.

Uses already-downloaded challenge samples only (PDF, DXF, STEP, smallest ZIP, one SLDPRT). Nothing is
downloaded, uploaded or committed. Runs in a fresh private (0700) directory with an ephemeral in-memory
secret; the catalog stores digests only, and no raw identity map is written. The report contains counts,
digests and pseudonym-free statistics only.
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from machina_deid import twopass as tp  # noqa: E402
from machina_deid.catalog import IdentityCatalog  # noqa: E402

BASE_PARENT = Path("/srv/new2/machina-deid/work")
MAX_ZIP_BYTES = 32 * 1024 * 1024


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def _first(root: Path, *patterns: str) -> Path | None:
    for pat in patterns:
        for p in sorted(root.rglob(pat)):
            if p.is_file() and not p.name.startswith("."):
                return p
    return None


def samples() -> list[tuple[Path, str]]:
    repo_input = Path(__file__).resolve().parents[1] / "input"
    picks = [
        ("pdf", _first(Path("/srv/new2/machina-deid/representative-pdf"), "*.PDF", "*.pdf")),
        # fallback: checksum-verified staged copy of the same real challenge DXF from the Phase 3 benchmark
        ("dxf", _first(Path("/srv/new2/machina-deid/representative-dxf"), "*.dxf", "*.DXF")
         or _first(Path("/srv/new2/machina-deid/work/phase3-benchmark/input"), "*.dxf")),
        ("step", _first(repo_input, "*.STEP", "*.stp", "*.step")),
        ("native", _first(repo_input, "*.SLDPRT")),
    ]
    zips = sorted((p for p in (repo_input / "archives").glob("*.zip") if p.stat().st_size <= MAX_ZIP_BYTES),
                  key=lambda p: p.stat().st_size)
    if zips:
        picks.append(("zip", zips[0]))
    return [(p, f"{label}/sample{p.suffix}") for label, p in picks if p is not None]


def main() -> int:
    t0 = time.monotonic()
    BASE_PARENT.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="phase5c-benchmark-", dir=BASE_PARENT))  # 0700
    out_dir = base / "output"
    files = samples()
    before = {rel: sha(p) for p, rel in files}
    secret = os.urandom(32)  # ephemeral; catalog is unusable after the run by design
    cat = IdentityCatalog(base / "catalog" / "catalog.sqlite", secret)
    try:
        t1 = time.monotonic()
        disc = tp.discover_batch(cat, files, limit=len(files), max_bytes=2 * 1024**3)
        t_disc = time.monotonic() - t1
        appr = cat.approvals_report()["counts"]

        t2 = time.monotonic()
        outcomes = []
        residual_rescan = 0
        for p, rel in files:
            dst = out_dir / rel
            o = tp.transform_file(cat, p, dst, rel)
            if o.output_written:  # independent check: rediscover in output, count still-approved values
                for d in tp.discover_file(dst, rel):
                    residual_rescan += sum(1 for c in d.candidates if cat.approved_pseudonym(c.value))
            outcomes.append(o)
        t_xf = time.monotonic() - t2
        after = {rel: sha(p) for p, rel in files}
        by_status: dict[str, int] = {}
        for o in outcomes:
            by_status[o.status] = by_status.get(o.status, 0) + 1
        report = {
            "inputs": [{"rel": rel, "bytes": p.stat().st_size, "source_sha256": before[rel][:16]} for p, rel in files],
            "discovery": {**disc.to_dict(), "elapsed_s": round(t_disc, 3)},
            "candidates_discovered": disc.candidates,
            "catalog_decisions": appr,
            "auto_approved": appr["approved"],
            "review_required": appr["review"],
            "transformation": {
                "by_status": by_status,
                "files_transformed": by_status.get("transformed", 0),
                "fields_replaced": sum(o.fields_replaced for o in outcomes),
                "residual_approved_transformer": sum(o.residual_approved for o in outcomes),
                "residual_approved_rescan": residual_rescan,
                "unsupported_files": by_status.get("unsupported", 0),
                "unsupported_zip_members": disc.unsupported,
                "failures": {o.path: o.reason for o in outcomes if o.status == "failed"},
                "per_file": [{k: v for k, v in o.to_dict().items() if k != "path"} | {"rel": o.path}
                             for o in outcomes],
                "elapsed_s": round(t_xf, 3),
            },
            "source_preserved": before == after,
            "raw_map_files_written": 0,
            "step_data_section": "analysis-only (not rewritten)",
            "elapsed_s": round(time.monotonic() - t0, 3),
            "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            "run_dir": str(base),
        }
    finally:
        cat.close()
    (base / "benchmark-report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["source_preserved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
