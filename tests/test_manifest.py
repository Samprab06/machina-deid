import pytest

from machina_deid import manifest as mf

TSV = (
    "# path\tbytes\tmd5\turl\n"
    "archives/a.zip\t1000\taaa\tcorpus/archives/a.zip\n"
    "pdf/x.pdf\t300\tbbb\tu\n"
    "pdf/y.pdf\t300\tbbb\tu\n"
    "pdf/z.pdf\t300\tbbb\tu\n"
    "cad/q.dxf\t200\tccc\tu\n"
    "cad/part.SLDPRT\t500\t\tu\n"
    "cad/empty.step\t0\tddd\tu\n"
    "bad line\n"
    "cad/" + "n" * 150 + ".dxf\t10\teee\tu\n"
)


@pytest.fixture
def rows(tmp_path):
    p = tmp_path / "m.tsv"
    p.write_text(TSV)
    return mf.parse_manifest(p)


def test_parse(rows):
    r, bad = rows
    assert len(r) == 8 and bad == 1
    assert r[5].ext == ".sldprt" and r[0].top_dir == "archives"


def test_parse_size():
    assert mf.parse_size("20GiB") == 20 * 1024**3
    assert mf.parse_size("1.5 MB") == int(1.5 * 1024**2)
    with pytest.raises(ValueError):
        mf.parse_size("lots")


def test_report_math_and_privacy(rows, tmp_path):
    r, bad = rows
    rep = mf.analyze(r, bad, reserves={str(tmp_path): 0}, batch_bytes=1000, top_n=3)
    assert rep["summary"] == {"files": 8, "bytes": 2610, "malformed_lines": 1, "empty_files": 1}
    ext = {e["ext"]: e for e in rep["by_extension"]}
    assert ext[".pdf"] == {"ext": ".pdf", "files": 3, "bytes": 900}
    assert rep["largest_files"][0]["bytes"] == 1000
    assert rep["archives"]["files"] == 1 and rep["archives"]["bytes"] == 1000
    d = rep["duplicates"]
    assert (d["groups"], d["redundant_files"], d["redundant_bytes"], d["blank_md5_files"]) == (1, 2, 600, 1)
    assert rep["long_paths"]["count"] == 1
    tiers = {p["ext"]: p["tier"] for p in rep["prioritization"]}
    assert tiers[".pdf"] == "P1" and tiers[".sldprt"] == "P3"
    assert "pdf/x.pdf" not in str(rep)  # no raw paths by default
    assert "path" in mf.analyze(r, bad, include_paths=True)["largest_files"][0]


def test_storage_projection(rows):
    r, _ = rows
    mounts = [{"path": "/m", "usable": 10_000, "free": 12_000, "reserve": 2_000}]
    pr = mf.project_usage(r, mounts, batch_bytes=1000, output_ratio=1.0, archive_expansion=3.0)
    share = 1000 / 2610
    mult = 1 + 1 + share * 3
    assert pr["assumptions"]["per_byte_multiplier"] == pytest.approx(mult, abs=1e-4)
    assert pr["batch_projected_usage"] == int(1000 * mult)
    m = pr["mounts"][0]
    assert m["batch_fits"] and m["max_safe_batch_bytes"] == int(10_000 / mult)
    assert pr["batches_for_corpus"] == 3 and pr["files_larger_than_batch"] == 0
    small = mf.project_usage(r, [{**mounts[0], "usable": 1500}], 1000)
    assert not small["mounts"][0]["batch_fits"]


def test_measure_mounts_reserve(tmp_path):
    m = mf.measure_mounts({str(tmp_path / "nonexistent"): 1 << 60})[0]
    assert m["free"] > 0 and m["usable"] == 0


def test_write_reports(rows, tmp_path):
    rep = mf.analyze(rows[0], reserves={str(tmp_path): 0})
    paths = mf.write_reports(rep, tmp_path / "work")
    assert {p.suffix for p in paths} == {".json", ".md"} and all(p.exists() for p in paths)
    assert "Prioritization" in paths[1].read_text()
