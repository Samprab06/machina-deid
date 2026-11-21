"""Phase 5G private review tooling: grouping, permissions, redaction, no approval side effects.

Synthetic values only (no real data)."""
import importlib.util
import io
import json
import os
import stat
from pathlib import Path

import pytest
from step_helpers import step_bytes

_SPEC = importlib.util.spec_from_file_location(
    "private_review", Path(__file__).resolve().parents[1] / "scripts" / "private_review.py")
pr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pr)
bm = pr.bm

SECRET = "ab" * 32
PERSON = "Zorblat Quenmore"  # synthetic
ORG = "Fictive Gadgetry Ltd"  # synthetic


def _bench(tmp_path, monkeypatch, files=None):
    work = tmp_path / "work"
    monkeypatch.setattr(pr, "WORK_ROOT", work)
    bench = bm._private_dir(work / "b")
    raw = bm._private_dir(bench / "raw")
    files = files or {
        "step-tiny-001.step": step_bytes(name="widget", author=f"('{PERSON}')", org=f"('{ORG}')"),
        "step-tiny-002.step": step_bytes(name="other", author=f"('  {PERSON.upper()} ')", org=f"('{ORG}')"),
    }
    sel_files, checks = [], {}
    for sid, data in files.items():
        p = raw / sid
        p.write_bytes(data)
        sel_files.append({"id": sid, "group": "step", "tier": "tiny", "source_kind": "github",
                          "manifest_path": f"sources/github/o/r/{sid}-src-name.step", "url_path": f"x/{sid}"})
        checks[sid] = {"path": str(p), "bytes": len(data), "sha256": bm.sha256_file(p), "md5": "",
                       "origin": "reused"}
    bm._write_private(bench / "selection.json", json.dumps({"version": 1, "files": sel_files, "reuse_roots": []}))
    bm._write_private(bench / "checksums.json", json.dumps(checks))
    bm._write_private(bench / "catalog.secret", SECRET + "\n")
    cdir = bm._private_dir(bench / "runs" / "r" / "catalog")
    cat = pr.IdentityCatalog(cdir / "catalog.sqlite", SECRET.encode())
    cat.close()
    return bench, cdir / "catalog.sqlite"


def _catalog(db):
    return pr.IdentityCatalog(db, SECRET.encode())


def test_groups_by_normalized_identity(tmp_path, monkeypatch):
    bench, db = _bench(tmp_path, monkeypatch)
    summary, private, out = pr.do_report("b", db, None, "t1")
    groups = private["groups"]
    with _catalog(db) as cat:
        dg = pr.digest(cat.value_key(PERSON))
        og = pr.digest(cat.value_key(ORG))
    person, org = groups[dg], groups[og]
    # Upper-cased, padded spelling merges with the original via catalog normalization.
    assert person["occurrences"] == 2 and len(person["raw_values"]) == 2
    assert person["source_samples"] == ["step-tiny-001.step", "step-tiny-002.step"]
    assert person["formats"] == {"step": 2} and person["confidences"] == {"high": 2}
    assert person["location_categories"] == {"step:FILE_NAME.author[]": 2}
    assert {e["location"] for e in person["evidence"]} == {"step:FILE_NAME.author[0]"}
    assert org["occurrences"] == 2 and org["catalog"]["pseudonym_status"] == "undecided"
    assert summary["groups"] == len(groups)
    assert {i["digest"] for i in summary["items"]} == set(groups)


def test_report_permissions_and_location(tmp_path, monkeypatch):
    bench, db = _bench(tmp_path, monkeypatch)
    _, _, out = pr.do_report("b", db, None, "t1")
    assert pr.bm._is_within(out, pr.WORK_ROOT.resolve())
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for f in ("private-review.json", "public-summary.json"):
        assert stat.S_IMODE((out / f).stat().st_mode) == 0o600
    with pytest.raises(SystemExit, match="not empty"):
        pr.do_report("b", db, None, "t1")
    with pytest.raises(SystemExit, match="inside"):
        pr._within_work(tmp_path / "elsewhere")
    with pytest.raises(SystemExit, match="does not exist"):
        pr.do_report("b", bench / "missing.sqlite", None, "t2")


def test_insecure_secret_and_changed_source_refused(tmp_path, monkeypatch):
    bench, db = _bench(tmp_path, monkeypatch)
    os.chmod(bench / "catalog.secret", 0o644)
    with pytest.raises(SystemExit, match="0600"):
        pr.do_report("b", db, None, "t1")
    os.chmod(bench / "catalog.secret", 0o600)
    (bench / "raw" / "step-tiny-001.step").write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="changed"):
        pr.do_report("b", db, None, "t1")


def test_public_summary_and_default_stdout_redacted(tmp_path, monkeypatch, capsys):
    bench, db = _bench(tmp_path, monkeypatch)
    assert pr.main(["report", "--bench", "b", "--catalog", str(db), "--name", "t1"]) == 0
    stdout = capsys.readouterr().out
    public = (bench / "review" / "t1" / "public-summary.json").read_text()
    private = (bench / "review" / "t1" / "private-review.json").read_text()
    for raw in (PERSON, PERSON.upper(), ORG, "widget", "src-name", str(tmp_path)):
        assert raw.lower() not in stdout.lower() and raw.lower() not in public.lower()
    assert PERSON in private and ORG in private  # raw values only in the protected report
    assert pr.main(["report", "--bench", "b", "--catalog", str(db), "--name", "t2", "--show-private"]) == 0
    assert PERSON in capsys.readouterr().out


def test_assert_public_fails_closed(tmp_path, monkeypatch):
    bench, db = _bench(tmp_path, monkeypatch)
    _, private, _ = pr.do_report("b", db, None, "t1")
    sel = bm.load_selection(bench)
    checks = json.loads((bench / "checksums.json").read_text())
    with pytest.raises(SystemExit, match="leak"):
        pr.assert_public({"items": [PERSON]}, private["groups"], sel, checks)


def test_report_has_no_approval_side_effect(tmp_path, monkeypatch):
    bench, db = _bench(tmp_path, monkeypatch)
    with _catalog(db) as cat:
        cat.reject("org", ORG, reason="synthetic rejection")
        before = (cat.approvals_report(), cat.report())
    _, private, _ = pr.do_report("b", db, None, "t1")
    with _catalog(db) as cat:
        assert (cat.approvals_report(), cat.report()) == before
        assert cat.approval_status("person", PERSON) is None
        og = pr.digest(cat.value_key(ORG))
    assert private["groups"][og]["catalog"]["decisions"] == {"org": "rejected"}
    assert private["groups"][og]["catalog"]["pseudonym_status"] == "not_approved"


def test_decide_argv_and_stdin(tmp_path, monkeypatch, capsys):
    bench, db = _bench(tmp_path, monkeypatch)
    _, private, out = pr.do_report("b", db, None, "t1")
    rpt = out / "private-review.json"
    with _catalog(db) as cat:
        dg, og = pr.digest(cat.value_key(PERSON)), pr.digest(cat.value_key(ORG))
    base = ["decide", "--bench", "b", "--catalog", str(db), "--report", str(rpt)]
    assert pr.main(base + ["--digest", dg, "--kind", "person", "--decision", "approve",
                           "--reason", "reviewed author field"]) == 0
    res = capsys.readouterr().out
    assert PERSON not in res and json.loads(res)[0]["status"] == "approved"
    monkeypatch.setattr("sys.stdin", io.StringIO(f"# comment\n{og} org reject not an identity here\n"))
    assert pr.main(base + ["--stdin"]) == 0
    assert ORG not in capsys.readouterr().out
    with _catalog(db) as cat:
        assert cat.approval_status("person", PERSON) == "approved"
        assert cat.approved_pseudonym(PERSON)[0] == "person"
        assert cat.approval_status("org", ORG) == "rejected"
    # Re-review reflects the new state; public summary still redacted.
    summary, private2, _ = pr.do_report("b", db, None, "t2")
    assert private2["groups"][dg]["catalog"]["pseudonym_status"] == "approved"
    assert PERSON not in json.dumps(summary)


def test_decide_validation(tmp_path, monkeypatch):
    bench, db = _bench(tmp_path, monkeypatch)
    _, _, out = pr.do_report("b", db, None, "t1")
    rpt = out / "private-review.json"
    with pytest.raises(SystemExit, match="digest"):
        pr.do_decide("b", db, None, rpt, [("zz", "person", "approve", "x")])
    with pytest.raises(SystemExit, match="not in the private"):
        pr.do_decide("b", db, None, rpt, [("0" * 16, "person", "approve", "x")])
    with pytest.raises(SystemExit, match="decision"):
        pr.do_decide("b", db, None, rpt, [("0" * 16, "person", "approved!", "x")])
    with pytest.raises(SystemExit, match="kind"):
        pr.do_decide("b", db, None, rpt, [("0" * 16, "human", "approve", "x")])
    with pytest.raises(SystemExit, match="expected"):
        pr.parse_decision_lines("abc person approve")
    os.chmod(rpt, 0o644)
    with pytest.raises(SystemExit, match="0600"):
        pr.do_decide("b", db, None, rpt, [])
    with pytest.raises(SystemExit):
        pr.main(["decide", "--bench", "b", "--catalog", str(db), "--report", str(rpt)])


def test_report_authentication_and_catalog_binding(tmp_path, monkeypatch):
    _, db = _bench(tmp_path, monkeypatch)
    _, report, out = pr.do_report("b", db, None, "t1")
    path = out / "private-review.json"
    dg = next(iter(report["groups"]))
    db2 = db.with_name("other.sqlite")
    _catalog(db2).close()
    decisions = [(dg, "person", "approve", "synthetic test")]
    with pytest.raises(SystemExit, match="different catalog"):
        pr.do_decide("b", db2, None, path, decisions)
    report["groups"][dg]["evidence"] = []
    path.write_text(json.dumps(report))
    with pytest.raises(SystemExit, match="authentication"):
        pr.do_decide("b", db, None, path, decisions)
    with _catalog(db) as cat:
        assert cat.approvals_report()["decisions"] == 0


def test_unsafe_paths_refused(tmp_path, monkeypatch):
    bench, db = _bench(tmp_path, monkeypatch)
    (_, report, out) = pr.do_report("b", db, None, "t1")
    path = out / "private-review.json"
    hardlink = out / "duplicate.json"
    os.link(path, hardlink)
    with pytest.raises(SystemExit, match="singly linked"):
        pr.do_decide("b", db, None, path, [])
    link = bench / "review" / "link"
    link.symlink_to(out, target_is_directory=True)
    with pytest.raises(SystemExit, match="unsafe work path"):
        pr.do_report("b", db, None, "link")
    rpt = out / "private-review.json"
    with pytest.raises(SystemExit, match="unsafe work path"):
        pr.do_decide("b", db, None, link / rpt.name, [])


def test_batch_prevalidated_and_no_cross_identity_reason_leak(tmp_path, monkeypatch):
    _, db = _bench(tmp_path, monkeypatch)
    _, _, out = pr.do_report("b", db, None, "t1")
    with _catalog(db) as cat:
        dg = pr.digest(cat.value_key(PERSON))
        og = pr.digest(cat.value_key(ORG))
    with pytest.raises(SystemExit, match="private string") as exc:
        pr.do_decide("b", db, None, out / "private-review.json",
                     [(og, "org", "approve", "reviewed structured field"),
                      (dg, "person", "approve", f"found with {ORG.upper()}")])
    assert ORG.upper() not in str(exc.value)
    with _catalog(db) as cat:
        assert cat.approvals_report()["decisions"] == 0


def test_cli_does_not_echo_bad_argument_values(capsys):
    with pytest.raises(SystemExit):
        pr.main(["decide", "--bench", "b", "--catalog", "private.db", "--report", "private.json",
                 "--kind", PERSON])
    captured = capsys.readouterr()
    assert PERSON not in captured.out + captured.err
