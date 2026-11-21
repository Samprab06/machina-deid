import json
import shutil

import pymupdf
import pytest

from machina_deid import pdf_lab
from machina_deid.pdf_eval import evaluate, holdout_split, load_labels
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


def test_value_validation():
    assert valid_value("person", "J. Smith") and valid_value("person", "Jane Q. Doe")
    for bad in ("2020-01-01", "N/A", "SCALE", "STEEL", "1:2", "-", "Ø120 ±0.05"):
        assert not valid_value("person", bad)
    assert valid_value("phone", "+44 1234 567890") and not valid_value("phone", "12.5 25.0")


def test_scan_finds_title_block_layout(corpus):
    _, inp, _ = corpus
    doc = pymupdf.open(inp / "famA/drw-001.pdf")
    f = scan_page_lines(page_lines(doc[0]))
    got = {(x.kind, x.source) for x in f}
    assert ("person", "label_right") in got and ("org", "label_right") in got
    assert ("person", "inline") in got and ("email", "pattern") in got
    assert not any(x.value.startswith("SCALE") for x in f)


def test_baseline_copy_scores_zero_with_fallback_class(corpus):
    tmp, inp, lp = corpus
    shutil.copytree(inp, tmp / "copy")
    rep = evaluate(load_labels(lp), inp, tmp / "copy")["all"]
    assert rep["scoped_removal"]["ok"] == 0 and set(rep["failures"]) == {"fallback_copy"}
    assert rep["same_identity"]["ok"] == 0


def test_holdout_keeps_duplicates_together(corpus):
    tmp, inp, lp = corpus
    labels = load_labels(lp)
    for seed in (b"a", b"b", b"c", b"d"):
        split = holdout_split(labels, inp, seed, 0.5)
        assert split["famA/drw-001.pdf"] == split["famB/copy-of-001.pdf"] == split["famB/sheet-3.pdf"]


def test_lab_refuses_private_writes_in_repo(corpus):
    tmp, inp, _ = corpus
    with pytest.raises(SystemExit):
        pdf_lab.main(["sample", "--input", str(inp), "--seed-file", str(tmp / "secret"),
                      "--out", str(pdf_lab.REPO / "list.txt")])



def test_survey_and_propose_cli(corpus, capsys):
    tmp, inp, _ = corpus
    pdf_lab.main(["survey", "--input", str(inp)])
    s = json.loads(capsys.readouterr().out)["survey"]
    assert s["docs"] == 4 and s["docs_with_role_labels"] == 4 and s["docs_with_email_shape"] == 2
    assert "Jane" not in json.dumps(s)  # aggregates only
    pdf_lab.main(["propose", "--input", str(inp), "--out", str(tmp / "draft.jsonl"), "--crops", str(tmp / "crops")])
    assert json.loads(capsys.readouterr().out)["proposed"] > 8
    rows = [json.loads(x) for x in (tmp / "draft.jsonl").read_text().splitlines()]
    assert all(r["status"] in ("proposed", "rejected") for r in rows)
    assert (tmp / "draft.jsonl").stat().st_mode & 0o077 == 0 and list((tmp / "crops").glob("*.png"))
    # proposed-but-unconfirmed rows are never scored
    assert load_labels(tmp / "draft.jsonl") == []


def test_distinct_triplets_count_collisions_and_failed_rewrites():
    from machina_deid.pdf_eval import Label, Occ, _metrics

    def occurrence(entity, token, *, ok=True, doc=None):
        label = Label(doc or entity, 0, (0, 0, 10, 10), entity, entity=entity)
        return Occ(label, ok, "" if ok else "rewrite", token)

    distinct = [occurrence("a", "one"), occurrence("b", "two"), occurrence("c", "three")]
    assert _metrics(distinct, {}, 100, 1)["distinct_identity_triplets"] == {"ok": 1, "total": 1, "rate": 1.0}
    for third in (occurrence("c", "one"), occurrence("c", "", ok=False)):
        result = _metrics([*distinct[:2], third], {}, 100, 1)
        assert result["distinct_identity_triplets"] == {"ok": 0, "total": 1, "rate": 0.0}


def test_same_identity_metric_requires_different_documents():
    from machina_deid.pdf_eval import Label, Occ, _metrics

    label = Label("same.pdf", 0, (0, 0, 10, 10), "original", entity="one")
    occurrence = Occ(label, True, "", "alias")
    assert _metrics([occurrence, occurrence], {}, 100, 1)["same_identity"]["total"] == 0
