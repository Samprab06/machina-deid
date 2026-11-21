"""Attempt 2b residual pilot: read-only, redacted, frozen-catalog only. Synthetic workspace."""
import json

import pytest
from production_fixtures import ORG, PERSON, file_set, make_workspace, tree_digest
from step_helpers import step_bytes
from test_attempt2_storage import cfg, snapshot

from machina_deid import production as pr
from machina_deid import residual_pilot as rp


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    files = file_set(3)
    for i in range(3):  # time_stamp hit: never edited -> a genuine residual-positive STEP row
        files[f"r/ts{i}.step"] = step_bytes(name=f"b{i}", author=f"('{PERSON}')", org=f"('{ORG}')",
                                            ts=f"{PERSON} {i}")
    files["r/pre.step"] = step_bytes(name="p", author=f"('{PERSON}')", org=f"('{ORG}')", preproc=f"{ORG} x")
    root = tmp_path_factory.mktemp("pilot")
    head = make_workspace(root, files)
    assert pr.run(cfg(root, head, len(files), "attempt-1"))["phase"] == "packaged"
    pr.adopt(cfg(root, head, len(files), "attempt-2", reuse_from="attempt-1"), "attempt-1")
    return root


def test_pilot_counts_only_and_read_only(ws, tmp_path, capsys):
    root = ws
    before = {d: snapshot(root / d) for d in ("input", "attempts/attempt-1")}
    ops = {k: v[0] for k, v in snapshot(root / "ops").items() if not k.endswith("runner.lock")}
    scratch = tmp_path / "scratch"
    assert rp.main(["--root", str(root), "--scratch", str(scratch), "--per-ext", "10"]) == 0
    text = capsys.readouterr().out
    rep = json.loads(text)
    assert rep["catalog_digest_verified"]
    step = rep["classes"]["residual"][".step"]["counts"]
    assert step["samples"] == 3 == step["source_preserved"] == step["ref_output_matches_ledger"]
    assert step["new_residual_total"] == 3 and step["old_residual_total"] == 3
    attr = rep["classes"]["residual"][".step"]["input_attribution"]
    assert attr["header:timestamp"] == 3
    assert "control" in rep["classes"]
    # redaction: no values, pseudonyms, or paths
    for bad in (PERSON, ORG, "Doe", "Acme", "proj", "ts0", "pre.step", str(root)):
        assert bad not in text
    assert not scratch.exists()
    assert {d: snapshot(root / d) for d in before} == before
    assert {k: v[0] for k, v in snapshot(root / "ops").items() if not k.endswith("runner.lock")} == ops
    assert tree_digest(root / "attempts/attempt-1/corpus")


@pytest.mark.parametrize("sub", ["input/x", "attempts/a", "ops/attempt-2/s"])
def test_scratch_inside_protected_tree_refused(ws, sub):
    with pytest.raises(SystemExit):
        rp.run_pilot(ws, "attempt-2", "attempt-1", ws / sub, 5)


def test_attribution_buckets(tmp_path):
    from machina_deid.catalog import IdentityCatalog
    from machina_deid import twopass as tp
    raw = step_bytes(name="b", author=f"('{PERSON}')", org=f"('{ORG}')", preproc=f"{ORG} x", data=(
        "DATA;\n#1=CARTESIAN_POINT('" + PERSON + "',(0.,0.,0.));\n"
        "#2=PRODUCT('P','" + PERSON + " part','',(#3));\n#10=ORGANIZATION('o','" + ORG + "','');\n"
        "ENDSEC;\nEND-ISO-10303-21;\n").encode())
    p = tmp_path / "a.stp"
    p.write_bytes(raw)
    with IdentityCatalog(tmp_path / "c.db", b"attempt2b-unit-secret-0123456789") as cat:
        tp.discover_batch(cat, [(p, "a.stp")], limit=1)
        a = rp.attribute_step(p, cat)
    assert a["header:preprocessor"] == 1 and a["header:author"] == 1 and a["header:organization"] == 1
    assert a["data:identity_position_exact"] == 1
    assert a["data:descriptive_scoped_boundary"] == 1
    assert a["data:other_entity:CARTESIAN_POINT"] == 1
