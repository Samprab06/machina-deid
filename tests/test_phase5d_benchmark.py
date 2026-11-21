"""Phase 5D benchmark tooling: deterministic selection, overlap refusal, private manifests, redacted report."""
import importlib.util
import json
import stat
from pathlib import Path

import pytest
from step_helpers import step_bytes

_SPEC = importlib.util.spec_from_file_location(
    "phase5d_benchmark", Path(__file__).resolve().parents[1] / "scripts" / "phase5d_benchmark.py")
bm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bm)


def _entry(fmt, tier, sha, path=None):
    return {"format": fmt, "tier": tier, "sha256": sha, "path": path or f"/x/{sha}.{fmt}", "bytes": 1}


def test_size_tiers_and_location_category():
    assert bm.size_tier(0) == "tiny" and bm.size_tier(64 * 1024) == "small"
    assert bm.size_tier(16 * 1024**2) == "large"
    assert bm.location_category("step:FILE_NAME.author[3]") == "step:FILE_NAME.author[]"
    assert bm.location_category("pdf:page[2].title_block[7]") == "pdf:page[].title_block[]"
    assert bm.location_category("weird value with spaces") == "other"
    assert bm.fmt_label(Path("a.STP")) == "step" and bm.fmt_label(Path("a.SLDPRT")) == "sldprt"


def test_selection_is_stratified_and_deterministic():
    pool = [_entry("sldprt", "small", f"{i:04x}") for i in range(50)]
    pool += [_entry("pdf", "small", "ff01"), _entry("dxf", "large", "ff02"), _entry("zip", "medium", "ff03")]
    a = bm.select([dict(e) for e in pool], 10)
    b = bm.select([dict(e) for e in reversed(pool)], 10)
    assert [e["path"] for e in a] == [e["path"] for e in b]
    assert {e["format"] for e in a} == {"sldprt", "pdf", "dxf", "zip"}  # rare strata always included
    assert len(a) == 10 and len({e["id"] for e in a}) == 10
    assert all("/" not in e["id"] for e in a)


def test_overlap_refused(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(SystemExit, match="overlap"):
        bm.check_no_overlap(src / "bench", [], [src])
    with pytest.raises(SystemExit, match="overlap"):
        bm.check_no_overlap(tmp_path, [src / "f.step"], [])
    with pytest.raises(SystemExit, match="Git repository"):
        bm.check_no_overlap(bm.REPO / "work" / "x", [], [])
    bm.check_no_overlap(tmp_path / "bench", [src / "f.step"], [src])


def test_freeze_and_run_end_to_end(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.step").write_bytes(step_bytes())
    (src / "dup.step").write_bytes(step_bytes())
    (src / "x.SLDPRT").write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 100)
    (src / "y.7z.resume-partial").write_bytes(b"partial")
    hidden = src / ".get-input"
    hidden.mkdir()
    (hidden / "manifest.tsv").write_text("secret\n")
    bench = tmp_path / "work" / "b1"

    s = bm.freeze(bench, [src], 50)
    assert s["selected"] == 3  # partial download and dot-directory excluded
    assert stat.S_IMODE(bench.stat().st_mode) == 0o700
    for f in ("manifest.json", "catalog.secret"):
        assert stat.S_IMODE((bench / f).stat().st_mode) == 0o600
    with pytest.raises(SystemExit, match="already exists"):
        bm.freeze(bench, [src], 50)

    rep = bm.run(bench, transform=True, run_name="r1")
    d = rep["baseline_discovery"]
    assert rep["source_preserved"] and rep["duplicate_source_sha_groups"] == 1
    assert d["files_by_format"] == {"sldprt": 1, "step": 2}
    assert d["unsupported_top_level_files"] == 1 and d["discovery_failures"] == 0
    assert d["auto_approval_eligible_candidates"] > 0 and d["review_required_candidates"] > 0
    assert d["candidates_by_location_category"].get("step:FILE_NAME.author[]", 0) >= 2
    assert rep["transformation"]["files_transformed"] == 2
    assert rep["transformation"]["residual_approved_rescan"] == 0
    assert stat.S_IMODE((bench / "runs").stat().st_mode) == 0o700
    public = (bench / "runs" / "r1" / "public-report.json").read_text()
    for raw in ("Jane Quincy Doe", "Acme Widgets", str(src), "a.step", "dup.step"):
        assert raw not in public
    with pytest.raises(SystemExit, match="not empty"):
        bm.run(bench, transform=False, run_name="r1")
    # frozen checksums are enforced
    (src / "a.step").write_bytes(step_bytes(author="('Someone Else')"))
    with pytest.raises(SystemExit, match="changed"):
        bm.run(bench, transform=False, run_name="r2")
