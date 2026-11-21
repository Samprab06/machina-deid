"""Phase 5E-A balanced benchmark: deterministic selection, byte cap, checksum/overlap protection, redaction."""
import hashlib
import importlib.util
import json
import stat
from pathlib import Path

import pytest
from step_helpers import step_bytes

_SPEC = importlib.util.spec_from_file_location(
    "phase5e_balanced_benchmark", Path(__file__).resolve().parents[1] / "scripts" / "phase5e_balanced_benchmark.py")
bm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bm)


def _row(path, n=1000, md5=""):
    return {"manifest_path": path, "bytes": n, "md5": md5, "url_path": "corpus/" + path}


def _pool():
    rows = []
    for i in range(40):
        rows.append(_row(f"sources/github/o{i % 7}/r{i}/part{i}.step", 500 + i * 4999))
        rows.append(_row(f"sources/gitlab/o{i}/r/d{i}.pdf", 2_000_000 + i))
        rows.append(_row(f"sources/web/s{i}/x/m{i}.stl", 100 + i))
    rows.append(_row("archives/a.zip", 10_000))
    rows.append(_row("sources/github/o/r/ignored.sldprt", 10))
    return rows


def test_selection_deterministic_and_balanced():
    t = {"step": 5, "pdf": 3, "zip": 2, "neutral_mesh": 4}
    a = bm.select(_pool(), t, byte_cap=bm.GIB, max_file=bm.GIB)
    b = bm.select(list(reversed(_pool())), t, byte_cap=bm.GIB, max_file=bm.GIB)
    assert [r["manifest_path"] for r in a] == [r["manifest_path"] for r in b]
    got = {g: sum(r["group"] == g for r in a) for g in t}
    assert got == {"step": 5, "pdf": 3, "zip": 1, "neutral_mesh": 4}  # only one zip available
    assert len({r["id"] for r in a}) == len(a) and all("/" not in r["id"] for r in a)
    steps = [r for r in a if r["group"] == "step"]
    assert len({bm.source_bucket(r["manifest_path"])[1] for r in steps}) == 5  # repo diversity
    assert len({r["tier"] for r in steps}) >= 2  # size diversity


def test_selection_prefers_local_rows():
    rows = _pool()
    local = {"sources/github/o3/r38/part38.step"}
    a = bm.select(rows, {"step": 1}, byte_cap=bm.GIB, max_file=bm.GIB, local=local)
    assert a[0]["manifest_path"] in local


def test_byte_cap_enforced():
    cap = 5_000_000
    a = bm.select(_pool(), {"step": 20, "pdf": 20, "neutral_mesh": 20}, byte_cap=cap, max_file=bm.GIB)
    assert sum(r["bytes"] for r in a) <= cap
    assert sum(r["group"] == "pdf" for r in a) == 2  # each ~2 MB
    assert all(r["bytes"] <= 1000 for r in bm.select(_pool(), {"step": 9}, byte_cap=bm.GIB, max_file=1000))
    with pytest.raises(SystemExit, match="hard limit"):
        bm.select(_pool(), {"step": 1}, byte_cap=bm.HARD_BYTE_CAP + 1, max_file=1)
    with pytest.raises(SystemExit):
        bm.main(["--select", "--byte-cap-gib", "9"])


def test_manifest_parsing_rejects_unsafe_paths(tmp_path):
    m = tmp_path / "manifest.tsv"
    m.write_text("# path\tbytes\tmd5\turl\n"
                 "sources/a/b.step\t10\tABCDEF0123456789abcdef0123456789\tcorpus/sources/a/b.step\n"
                 "../etc/passwd\t10\t\tcorpus/x\n/abs.pdf\t5\t\tcorpus/abs.pdf\nzero.pdf\t0\t\tc/zero.pdf\n")
    rows = bm.read_manifest(m)
    assert [r["manifest_path"] for r in rows] == ["sources/a/b.step"]
    assert rows[0]["md5"] == "abcdef0123456789abcdef0123456789"


def test_overlap_and_protected_bench(tmp_path):
    with pytest.raises(SystemExit, match="Git repository"):
        bm.check_no_overlap(bm.REPO / "work" / "x")
    with pytest.raises(SystemExit, match="protected"):
        bm.check_no_overlap(tmp_path / "phase5d-frozen")
    with pytest.raises(SystemExit, match="overlap"):
        bm.check_no_overlap(tmp_path / "b", [tmp_path / "b" / "runs" / "r" / "x.step"], [tmp_path / "b" / "runs" / "r"])
    with pytest.raises(SystemExit):
        bm.main(["--run", "--bench", "phase5d-frozen"])


def _setup(tmp_path):
    reuse = tmp_path / "reuse"
    data = step_bytes()
    (reuse / "sources/github/o/r").mkdir(parents=True)
    (reuse / "sources/github/o/r/local.step").write_bytes(data)
    remote = {"sources/github/p/q/remote-part.step": step_bytes(author="('Other Person')"),
              "sources/web/z/y/mesh-model.stl": b"solid x\nendsolid x\n"}
    lines = ["# path\tbytes\tmd5\turl"]
    for p, b in [("sources/github/o/r/local.step", data), *remote.items()]:
        lines.append(f"{p}\t{len(b)}\t{hashlib.md5(b).hexdigest()}\tcorpus/{p}")
    m = tmp_path / "manifest.tsv"
    m.write_text("\n".join(lines) + "\n")
    calls = []

    def fake_fetch(url, dst, n, *, timeout_s):
        calls.append(url)
        dst.write_bytes(remote[url.split("/corpus/", 1)[1]])
    return reuse, m, remote, calls, fake_fetch


def test_end_to_end_checksums_and_redaction(tmp_path):
    reuse, m, remote, calls, fake = _setup(tmp_path)
    bench = tmp_path / "work" / "b"
    s = bm.do_select(bench, m, {"step": 5, "neutral_mesh": 1}, byte_cap=bm.GIB, max_file=bm.GIB,
                     reuse_roots=[reuse])
    assert s["selected"] == 3 and s["reusable_locally"] == 1
    with pytest.raises(SystemExit, match="already exists"):
        bm.do_select(bench, m, {"step": 1}, byte_cap=bm.GIB, max_file=bm.GIB, reuse_roots=[reuse])
    f = bm.do_fetch(bench, base="https://mirror.invalid", fetcher=fake)
    assert f["reused"] == 1 and f["downloaded"] == 2 and len(calls) == 2  # only selected missing files
    assert stat.S_IMODE(bench.stat().st_mode) == 0o700
    for name in ("selection.json", "checksums.json", "checksums.sha256", "catalog.secret"):
        assert stat.S_IMODE((bench / name).stat().st_mode) == 0o600
    bm.do_fetch(bench, base="https://mirror.invalid", fetcher=fake)
    assert len(calls) == 2  # idempotent

    rep = bm.do_run(bench, transform=True, run_name="r1")
    d = rep["baseline_discovery"]
    assert rep["source_preserved"]
    st = d["by_format"]["step"]
    assert st["selected"] == 2 and st["readable"] == 2 and st["reused"] == 1 and st["downloaded"] == 1
    assert st["auto_eligible"] > 0 and st["review_required"] > 0 and st["confident_identities"] > 0
    # attempt 2b: STL is now read for pass-2 header lookups (readable, never auto-approved)
    assert d["by_format"]["neutral_mesh"]["unsupported"] == 0 and d["by_format"]["neutral_mesh"]["readable"] == 1
    assert isinstance(rep["transformation"], dict)
    public = (bench / "runs" / "r1" / "public-report.json").read_text()
    for raw in ("Jane Quincy Doe", "Other Person", "Acme Widgets", str(tmp_path), "local.step",
                "remote-part.step", "mesh-model.stl", "sources/github"):
        assert raw not in public

    # frozen checksum enforced
    p = Path(json.loads((bench / "checksums.json").read_text())["step-tiny-001.step"]["path"])
    p.chmod(0o600)
    p.write_bytes(p.read_bytes().replace(b"Jane", b"Joan"))
    with pytest.raises(SystemExit, match="changed"):
        bm.do_run(bench, transform=False, run_name="r2")


def test_fetch_rejects_md5_mismatch(tmp_path):
    reuse, m, remote, calls, _ = _setup(tmp_path)
    bench = tmp_path / "b"
    bm.do_select(bench, m, {"neutral_mesh": 1}, byte_cap=bm.GIB, max_file=bm.GIB, reuse_roots=[reuse])

    def bad(url, dst, n, *, timeout_s):
        dst.write_bytes(b"X" * n)
    f = bm.do_fetch(bench, base="https://mirror.invalid", fetcher=bad)
    assert f["frozen"] == 0 and f["failed:md5_mismatch"] == 1
    assert not list((bench / "raw").iterdir())
    with pytest.raises(SystemExit, match="https"):
        bm.do_fetch(bench, base="http://mirror.invalid", fetcher=bad)


def test_redaction_guard_fails_closed():
    sel = {"files": [_row("sources/github/o/r/secret-name.step")], "reuse_roots": []}
    with pytest.raises(SystemExit, match="leak"):
        bm.assert_redacted({"x": "sources/github/o/r/secret-name.step"}, sel, {})
    bm.assert_redacted({"x": 1}, sel, {})


def test_redaction_guard_allows_opaque_id_equal_to_basename():
    sel = {"files": [_row("sources/github/o/r/step-large-150.step")], "reuse_roots": []}
    # The public report uses an opaque selection ID; equality with a source basename
    # must not be treated as a leak when the full source path is absent.
    bm.assert_redacted({"per_file": [{"id": "step-large-150.step"}]}, sel, {})


def test_safe_rel_allows_unicode_but_not_traversal():
    assert bm.safe_rel("sources/catalog/ASTM D638 [v1]/cuña.pdf")
    assert bm.safe_rel("corpus/sources/catalog/ASTM%20D638/cun%CC%83a.pdf", url=True)
    assert not bm.safe_rel("corpus/a b.pdf", url=True)
    for bad in ("../x.pdf", "a/../x", "/abs", "a//b", "a\\b", "a\nb"):
        assert not bm.safe_rel(bad)
