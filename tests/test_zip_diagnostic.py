"""Attempt 2c ZIP residual diagnostic: read-only, redacted, frozen catalog. Synthetic only."""
import io
import json
import zipfile

import pytest
from production_fixtures import ORG, PERSON, file_set, make_workspace
from step_helpers import step_bytes
from test_attempt2_storage import cfg, snapshot

from machina_deid import production as pr
from machina_deid import zip_diagnostic as zd
from machina_deid.idmap import IdentityMap


def _zip(members):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in members.items():
            z.writestr(n, d)
    return b.getvalue()


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    files = file_set(2)
    stp = step_bytes(name="b", author=f"('{PERSON}')", org=f"('{ORG}')", ts=f"{PERSON} 1")
    files["z/a.zip"] = _zip({"d/m.step": stp, "notes.txt": f"owner {PERSON}\n".encode(),
                             "blob.bin": b"\x00\x01" + PERSON.encode() + b"\xff"})
    files["z/b.zip"] = _zip({"x.step": stp})
    root = tmp_path_factory.mktemp("zipdiag")
    head = make_workspace(root, files)
    assert pr.run(cfg(root, head, len(files), "attempt-1"))["phase"] == "packaged"
    pr.adopt(cfg(root, head, len(files), "attempt-2", reuse_from="attempt-1"), "attempt-1")
    return root


def test_diagnostic_classifies_and_is_read_only(ws, tmp_path, capsys):
    before = {d: snapshot(ws / d) for d in ("input", "attempts/attempt-1")}
    scratch = tmp_path / "s"
    assert zd.main(["--root", str(ws), "--scratch", str(scratch), "--limit", "10"]) == 0
    text = capsys.readouterr().out
    rep = json.loads(text)
    t = rep["totals"]
    assert t["archives"] == 2 == t["source_preserved"] == t["ref_output_matches_ledger"]
    assert t["ledger_old_residual_total"] > 0
    assert t["new_reported_residual_total"] == t["new_legacy_scan_total"]  # ascii values: semantics agree
    assert t.get("reported_vs_strict_rescan_delta", 0) == 0
    ev = rep["evidence"]
    assert ev["confirmed:step_literal"] >= 2  # timestamp header hits, parsed
    assert ev["confirmed:text_member"] >= 1
    assert ev["unknown:raw_only"] >= 1  # binary blob: never claimed real
    # every reported hit is in exactly one evidence bucket
    assert sum(ev.values()) == t["new_reported_residual_total"]
    assert any(k.startswith("step_header:timestamp") for k in rep["step_location"])
    assert any(k.startswith(".txt|unsupported_passthrough") for k in rep["by_member"])
    for bad in (PERSON, ORG, "Doe", "Acme", "notes", "m.step", "a.zip", str(ws)):
        assert bad not in text
    assert not scratch.exists()
    assert {d: snapshot(ws / d) for d in before} == before


def test_hit_evidence_overlap_and_pseudonym_artifacts():
    m = IdentityMap(strings={"Jane Doe": "Person-Doe1", "Doe": "Person-X"})
    data = b"Jane Doe | Doe | Person-Doe1"
    ev = zd.hit_evidence(data, m)
    short = ev[1]
    assert short["artifact:overlap_longer_value"] == 1
    assert short["artifact:inside_pseudonym"] == 1
    assert short["candidate"] == 1 and short["boundary:word_bounded"] == 1
    assert ev[0]["candidate"] == 1
    # artifacts + candidates == strict scan
    from machina_deid import archive as ar
    assert {i: sum(c[k] for k in c if k.startswith(("artifact:", "candidate"))) for i, c in ev.items()} \
        == ar._scan(data, m)


def test_hit_evidence_embedded_boundary_and_utf16():
    m = IdentityMap(strings={"Ann": "P1"})
    assert zd.hit_evidence(b"Annex Ann", m)[0]["boundary:embedded_in_word"] == 1
    ev = zd.hit_evidence("x Ann y".encode("utf-16-le"), m)[0]
    assert ev["encoding:utf-16-le"] == 1 and ev["boundary:word_bounded"] == 1
