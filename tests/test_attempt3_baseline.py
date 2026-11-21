"""RCAC Attempt 3 independent baseline: run the submitted revision against the frozen attempt3 truth fixture.

Truth lives in tests/attempt3_truth.json (frozen before reading any other agent's changes).
All values are fictional. The test checks what the truth says and nothing else:
  * every enumerated identity occurrence is gone from the output (text, metadata, annots),
  * same-identity occurrences (in-doc repeats and cross-document) share one replacement,
  * the unrelated similar name and all technical strings are preserved verbatim.
Failures are recorded, not fixed by editing the truth.
"""
import json
import sqlite3
from pathlib import Path

import pymupdf
import pytest

from production_fixtures import make_workspace
from machina_deid import production as pr

TRUTH = json.loads((Path(__file__).parent / "attempt3_truth.json").read_text())
P1, P2 = TRUTH["identities"]["P1"], TRUTH["identities"]["P2"]
O1, E1 = TRUTH["identities"]["O1"], TRUTH["identities"]["E1"]
CONF = TRUTH["confusable"]["C1"]["value"]
ABS = TRUTH["must_be_absent"]
KEEP = TRUTH["must_be_preserved"]


def _pdf(root, rel, draw, meta=None):
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    draw(p)
    if meta:
        d.set_metadata(meta)
    path = root / "gen" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)
    d.close()
    return path.read_bytes()


def build_corpus(root: Path) -> dict[str, bytes]:
    def doc1(p):
        for y, label, val in ((480, "DRAWN", P1), (460, "CHECKED", P2), (440, "COMPANY", O1)):
            p.insert_text((500, y), label, fontsize=7)
            p.insert_text((560, y), val, fontsize=8)
        p.insert_text((100, 300), f"PREPARED BY: {P1} AND {CONF}", fontsize=9)
        p.insert_text((100, 280), "DWG 77-042   SCALE 1:2", fontsize=9)
        p.insert_text((100, 260), "Ø120 ±0.05", fontsize=8)

    def doc2(p):
        p.insert_text((60, 60), f"NOTES: questions to {P1}", fontsize=8)
        p.insert_text((60, 40), E1, fontsize=6)
        p.insert_text((100, 280), "DWG 77-042   SCALE 1:2", fontsize=9)
        a = p.add_text_annot((700, 50), "review")
        a.set_info(title=P1)
        a.update()

    files = {
        "famA/drw-001.pdf": _pdf(root, "famA/drw-001.pdf", doc1),
        "famA/drw-002.pdf": _pdf(root, "famA/drw-002.pdf", doc2, meta={"author": P1, "title": "Bracket"}),
    }
    return files


def cfg(root, head, n):
    return pr.Config(root=root, attempt="attempt-1", expected_rows=n, expected_revision=head,
                     workers=2, reserve_bytes=0, poll_seconds=0.05, batch_files=4, heavy_slots=1)


def open_pdf(data: bytes):
    return pymupdf.open(stream=data, filetype="pdf")


def lines_of(data: bytes):
    d = open_pdf(data)
    out = []
    for p in d:
        for b in p.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                out.append("".join(s["text"] for s in l["spans"]))
    d.close()
    return out


def full_text(data: bytes) -> str:
    d = open_pdf(data)
    t = "\n".join(p.get_text() for p in d) + str(d.metadata)
    d.close()
    return t


def line(data: bytes, prefix: str) -> str | None:
    for l in lines_of(data):
        if l.startswith(prefix):
            return l
    return None


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    root = tmp_path_factory.mktemp("a3")
    files = build_corpus(root)
    head = make_workspace(root, files)
    out = pr.run(cfg(root, head, len(files)))
    return root, files, out


def test_a3_truth_run(produced):
    root, files, out = produced
    assert out["phase"] == "packaged", out
    corp = root / "attempts/attempt-1/corpus"
    led = sqlite3.connect(root / "ops/attempt-1/ledger.sqlite")
    status = dict(led.execute("SELECT path, status FROM output"))

    o1 = (corp / "famA/drw-001.pdf").read_bytes()
    o2 = (corp / "famA/drw-002.pdf").read_bytes()
    for rel, o in (("famA/drw-001.pdf", o1), ("famA/drw-002.pdf", o2)):
        assert status[rel] == "transformed", (rel, status[rel])
        t = full_text(o)
        for v in ABS:
            assert v not in t, (rel, "original leaked:", v)

    # confusable similar name preserved verbatim
    assert CONF in full_text(o1), "confusable name was altered"
    # technical strings preserved
    assert KEEP["DWG 77-042"] and "DWG 77-042" in full_text(o1) and "DWG 77-042" in full_text(o2)
    assert "SCALE 1:2" in full_text(o1) and "SCALE 1:2" in full_text(o2)
    assert "Ø120 ±0.05" in full_text(o1)

    # same identity -> one shared replacement across in-doc repeats and across documents
    drawn = line(o1, "DRAWN") or ""
    prep = line(o1, "PREPARED BY: ") or ""
    note = line(o2, "NOTES: questions to ") or ""
    drawn_val = drawn[len("DRAWN"):].strip()
    prep_val = prep[len("PREPARED BY:"):].replace(f"AND {CONF}", "").strip()
    note_val = note[len("NOTES: questions to "):].strip()
    assert prep_val, "prepared-by replacement missing"
    assert note_val, "notes replacement missing"
    assert drawn_val == prep_val == note_val, (drawn_val, prep_val, note_val)
    assert drawn_val not in (P1, CONF)

    # annotation title: same identity, must not keep the original
    d2 = open_pdf(o2)
    for p in d2:
        for a in p.annots() or []:
            assert a.info.get("title") not in (P1, None), a.info.get("title")
            assert a.info["title"] == note_val, (a.info["title"], note_val)
    d2.close()
    # email replaced (in doc2 inline)
    assert E1 not in full_text(o2)
    # metadata author
    d2 = open_pdf(o2)
    assert d2.metadata.get("author") == note_val, d2.metadata.get("author")
    d2.close()
    # email replaced
    d1 = open_pdf(o1)
    assert d1.metadata.get("author") in (None, "")
    d1.close()
