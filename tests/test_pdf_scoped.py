import json
import shutil

import pymupdf
import pytest

from machina_deid import pdf_lab
from machina_deid.pdf_entities import EntityMapBuilder, Resolver, fitted
from machina_deid.pdf_eval import canonical_token, evaluate, holdout_split, load_labels
from machina_deid.pdf_scoped import transform_pdf_scoped
from machina_deid.pdf_titleblock import page_lines, scan_page_lines, valid_value

from pdf_fixtures import make_corpus, write_labels

SECRET = b"synthetic-test-secret-0123456789"


@pytest.fixture()
def corpus(tmp_path):
    inp = tmp_path / "in"
    labels = make_corpus(inp)
    lp = tmp_path / "labels.jsonl"
    write_labels(labels, lp)
    docs = sorted(str(p.relative_to(inp)) for p in inp.rglob("*.pdf"))
    (tmp_path / "list.txt").write_text("\n".join(docs) + "\n")
    (tmp_path / "secret").write_bytes(SECRET)
    return tmp_path, inp, lp


def _run(tmp, inp, capsys):
    pdf_lab.main(["build-map", "--input", str(inp), "--list", str(tmp / "list.txt"), "--secret-file",
                  str(tmp / "secret"), "--map", str(tmp / "map.sqlite")])
    capsys.readouterr()
    pdf_lab.main(["run", "--input", str(inp), "--list", str(tmp / "list.txt"), "--output", str(tmp / "out"),
                  "--secret-file", str(tmp / "secret"), "--map", str(tmp / "map.sqlite")])
    return json.loads(capsys.readouterr().out)["run"]


def test_end_to_end_meets_targets(corpus, capsys):
    tmp, inp, lp = corpus
    run = _run(tmp, inp, capsys)
    assert run["transformed"] == 4 and run["edits_planned"] == run["edits_verified"]
    rep = evaluate(load_labels(lp), inp, tmp / "out", trace_path=tmp / "out/trace.jsonl")["all"]
    assert rep["scoped_removal"]["rate"] == 1.0, rep
    assert rep["same_identity"]["rate"] == 1.0, rep
    assert rep["cross_document_triplets"]["total"] > 0 and rep["cross_document_triplets"]["rate"] == 1.0
    assert rep["distinctness_collisions"] == 0
    assert rep["engineering"]["docs_ok"] == rep["engineering"]["docs"], rep["engineering"]
    out = pymupdf.open(tmp / "out/famA/drw-001.pdf")
    assert not out.metadata["author"].startswith("Jane") and out.metadata["author"].startswith("Person-")
    assert "SCALE 1:2" in out[0].get_text() and "Ø120 ±0.05" in out[0].get_text()
    ann = [a.info for a in pymupdf.open(tmp / "out/famA/drw-002.pdf")[0].annots()]
    assert ann and "Bob Builder" not in ann[0]["content"] and "Bob Builder" not in ann[0]["title"]


def test_evaluator_flags_pipeline_claim_it_disagrees_with(corpus, capsys):
    tmp, inp, lp = corpus
    _run(tmp, inp, capsys)
    # Sabotage: put the original back on top of one verified edit; trace still says "verified".
    p = tmp / "out/famB/sheet-3.pdf"
    d = pymupdf.open(p)
    d[0].insert_text((100, 70), "Carla Tester", fontsize=5)
    d.save(tmp / "x.pdf")
    d.close()
    (tmp / "x.pdf").replace(p)
    labels = load_labels(lp)
    for lb in labels:
        if lb.doc == "famB/sheet-3.pdf":
            lb.bbox = (100, 64.6, lb.bbox[2], 71.5)
    rep = evaluate(labels, inp, tmp / "out", trace_path=tmp / "out/trace.jsonl")["all"]
    assert rep["failures"].get("discovery", 0) + rep["failures"].get("validation", 0) >= 1


def test_missed_label_is_classified_discovery(corpus, capsys):
    tmp, inp, lp = corpus
    _run(tmp, inp, capsys)
    labels = load_labels(lp)
    labels[0].bbox = (300, 100, 340, 110)  # an occurrence the pipeline never saw
    labels[0].text = "NOT THERE"
    rep = evaluate(labels[:1], inp, tmp / "out", trace_path=tmp / "out/trace.jsonl")["all"]
    assert rep["failures"] == {"discovery": 1}


def test_frozen_map_aliases_and_ambiguity(tmp_path):
    from machina_deid.pdf_titleblock import Finding
    b = EntityMapBuilder(SECRET)
    b.add_document("fam/a.pdf", [Finding(0, "person", "Jane Doe", "drawn", "label_right"),
                                 Finding(0, "org", "Acme Ltd", "company", "label_right"),
                                 Finding(0, "person", "Morgan", "drawn", "label_right")])
    b.add_document("fam/b.pdf", [Finding(0, "person", "J. Doe", "drawn", "label_right"),
                                 Finding(0, "org", "ACME LIMITED", "company", "label_right"),
                                 Finding(0, "org", "Morgan", "company", "label_right")])
    stats = b.freeze(tmp_path / "m.sqlite")
    assert stats["alias:initial_surname"] == 1 and stats["alias:org_suffix"] == 1 and stats["ambiguous"] == 1
    with pytest.raises(Exception):
        b.freeze(tmp_path / "m.sqlite")  # frozen maps are never overwritten
    r = Resolver(SECRET, tmp_path / "m.sqlite")
    assert r.resolve("person", "J. Doe")[0].pseudonym == r.resolve("person", "jane  doe")[0].pseudonym
    assert r.resolve("org", "Acme Ltd")[0].pseudonym == r.resolve("org", "ACME LIMITED")[0].pseudonym
    assert r.resolve("person", "Morgan") == (None, "ambiguous_identity")
    assert r.resolve("person", "Unknown Person") == (None, "not_in_frozen_map")
    with pytest.raises(Exception):
        Resolver(b"another-secret-0123456789abcdef", tmp_path / "m.sqlite")


def test_fitted_form_keeps_token_and_kind():
    assert fitted("Person-1A2B3", "person") == "PE-1A2B3"
    assert fitted("Project-1A2B3", "project") == "PJ-1A2B3"
    assert canonical_token("PE-1A2B3") == canonical_token("Person-1A2B3") == "1A2B3"


def test_no_findings_means_no_output(tmp_path):
    d = pymupdf.open()
    d.new_page().insert_text((50, 50), "GENERAL NOTES  SCALE 1:1", fontsize=8)
    d.save(tmp_path / "a.pdf")
    r = transform_pdf_scoped(tmp_path / "a.pdf", tmp_path / "b.pdf", Resolver(SECRET, derive=True))
    assert not r.ok and r.reason == "no_resolved_identities" and not (tmp_path / "b.pdf").exists()