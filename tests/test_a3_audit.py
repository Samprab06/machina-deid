"""Synthetic check of the independent audit script (MuPDF extractor stand-in for Poppler)."""
import importlib.util
import json
import shutil
from pathlib import Path

import pymupdf

spec = importlib.util.spec_from_file_location("a3a", Path(__file__).parents[1] / "scripts/a3_poppler_audit.py")
a3a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a3a)


def _pdf(path, lines):
    d = pymupdf.open()
    p = d.new_page()
    for i, t in enumerate(lines):
        p.insert_text((60, 80 + 20 * i), t, fontsize=9)
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)


def test_audit_scores_packages_and_exports_counts_only(tmp_path):
    src, a, b = tmp_path / "src", tmp_path / "pkgA", tmp_path / "pkgB"
    _pdf(src / "f/1.pdf", ["DRAWN BY: Ana Duarte      SCALE 1:2", "contact ana.d@corp-x.example",
                           "DRAWN TO SCALE", "CHECKED: Mo Reyes"])
    _pdf(src / "f/2.pdf", ["APPROVED Ana Duarte", "COMPANY: Duarte Fabrication"])
    shutil.copytree(src, a)  # passthrough package
    _pdf(b / "f/1.pdf", ["DRAWN BY: Person-1      SCALE 1:2", "contact user-9@example.invalid",
                         "DRAWN TO SCALE", "CHECKED: Person-2"])
    # b/f/2.pdf missing -> stays in denominator
    rows = [{"path": "f/1.pdf", "split": "development", "family": "f"},
            {"path": "f/2.pdf", "split": "development", "family": "f"},
            {"path": "f/3.pdf", "split": "holdout", "family": "f"}]
    man = tmp_path / "m.json"
    man.write_text(json.dumps(rows))
    out = tmp_path / "agg.json"
    a3a.main(["full", "--source", str(src), "--manifest", str(man), "--package", f"A={a}", "--package",
              f"B={b}", "--extractor", "mupdf", "--out", str(out)])
    rep = json.loads(out.read_text())
    assert rep["rows"] == 2 and rep["occurrences"] == 5 and rep["repeated_groups"] == 1
    assert rep["packages"]["A"]["original_present"] == 5 and rep["packages"]["A"]["removed"] == 0
    pb = rep["packages"]["B"]
    assert pb["removed"] == 3 and pb["output_missing_or_unreadable"] == 2 and pb["visible_replaced"] == 2
    assert pb["replacement_collisions"] == 0
    text = out.read_text()
    for secret in ("Duarte", "Reyes", "corp-x", "f/1.pdf"):
        assert secret not in text
