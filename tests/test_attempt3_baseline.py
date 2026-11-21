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
import zipfile
from pathlib import Path

import pymupdf
import pytest

from ole_helpers import cfb, cstring, summary_info
from production_fixtures import make_workspace, zip_bytes
from machina_deid import ole_transform as ot
from machina_deid import production as pr

TRUTH = json.loads((Path(__file__).parent / "attempt3_truth.json").read_text())
P1, P2 = TRUTH["identities"]["P1"], TRUTH["identities"]["P2"]
O1, E1 = TRUTH["identities"]["O1"], TRUTH["identities"]["E1"]
CONF = TRUTH["confusable"]["C1"]["value"]
ACCOUNT = TRUTH["ole"]["A1"]
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


def ole_bytes(account=ACCOUNT, extra=b""):
    body = (b"\x00" * 16 + cstring(account) + b"\x00" * 8
            + f"C:\\Users\\{account}\\Parts\\a.sldprt".encode("utf-16-le") + b"\x00\x00" + extra)
    return cfb({"\x05SummaryInformation": summary_info({8: (0x1E, account)}), "Contents": body,
                "PreviewPNG": b"\x89PNG preview bytes " + f"C:\\Users\\{account}\\x".encode("utf-16-le")})


def ole_props(data: bytes) -> list[str]:
    return [p.value for p in ot.summary_props(ot.parse(data))]


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

    d1 = _pdf(root, "famA/drw-001.pdf", doc1)
    d2 = _pdf(root, "famA/drw-002.pdf", doc2, meta={"author": P1, "title": "Bracket"})

    # rotated page: identity inline + confusable + drawing number
    def doc3(p):
        p.insert_text((50, 300), "DWG 77-042", fontsize=9)
        p.insert_text((50, 280), f"APPROVED {P1} (not {CONF})", fontsize=9)

    d3_doc = pymupdf.open()
    p3 = d3_doc.new_page(width=595, height=842)
    p3.set_rotation(90)
    doc3(p3)
    path = root / "gen" / "famB/sheet-3.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    d3_doc.save(path)
    d3_doc.close()
    d3 = path.read_bytes()

    return {
        "famA/drw-001.pdf": d1,
        "famA/drw-002.pdf": d2,
        "famB/sheet-3.pdf": d3,
        "cad/part1.sldprt": ole_bytes(),
        "cad/part2.sldprt": ole_bytes(extra=b"\x01" * 40),
        "bundle/pack.zip": zip_bytes([("drw/drw-002.pdf", d2), ("cad/asm.sldasm", ole_bytes()),
                                      ("readme.txt", b"plain notes")]),
    }


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


def _outputs(produced):
    root, files, out = produced
    corp = root / "attempts/attempt-1/corpus"
    led = sqlite3.connect(root / "ops/attempt-1/ledger.sqlite")
    status = dict(led.execute("SELECT path, status FROM output"))
    return out, status, files, {rel: (corp / rel).read_bytes()
                                for rel in ("famA/drw-001.pdf", "famA/drw-002.pdf", "famB/sheet-3.pdf",
                                            "cad/part1.sldprt", "cad/part2.sldprt", "bundle/pack.zip")}


def test_a3_status_transformed(produced):
    out, status, _, _ = _outputs(produced)
    assert out["phase"] == "packaged", out
    assert status["famA/drw-001.pdf"] == "transformed", status
    assert status["famA/drw-002.pdf"] == "transformed", status
    assert status["famB/sheet-3.pdf"] == "transformed", status
    assert status["cad/part1.sldprt"] == "transformed", status
    assert status["cad/part2.sldprt"] == "transformed", status


def test_a3_originals_absent(produced):
    """Truth: all four PDF originals must be gone from every PDF output (top level, zip member)."""
    _, status, _, data = _outputs(produced)
    for rel in ("famA/drw-001.pdf", "famA/drw-002.pdf", "famB/sheet-3.pdf"):
        if status[rel] != "transformed":
            pytest.skip(f"{rel} failed closed (see test_a3_status_transformed)")
        for v in ABS:
            assert v not in full_text(data[rel]), (rel, "original leaked:", v)
    with zipfile.ZipFile(data["bundle/pack.zip"]) as z:
        for v in ABS:
            assert v not in full_text(z.read("drw/drw-002.pdf")), ("zip member", "original leaked:", v)


def test_a3_confusable_preserved(produced):
    """Truth: the unrelated similar name must survive verbatim (upright doc + rotated page)."""
    _, status, _, data = _outputs(produced)
    assert CONF in full_text(data["famA/drw-001.pdf"]), "confusable name was altered (doc1)"
    if status["famB/sheet-3.pdf"] == "transformed":
        assert CONF in full_text(data["famB/sheet-3.pdf"]), "confusable name was altered (rotated page)"


def test_a3_technical_preserved(produced):
    """Truth: drawing number, scale and tolerance survive (rotated page must keep DWG number)."""
    _, status, _, data = _outputs(produced)
    t1, t2 = full_text(data["famA/drw-001.pdf"]), full_text(data["famA/drw-002.pdf"])
    assert "DWG 77-042" in t1 and "DWG 77-042" in t2
    assert "SCALE 1:2" in t1 and "SCALE 1:2" in t2
    assert "Ø120 ±0.05" in t1
    if status["famB/sheet-3.pdf"] == "transformed":
        assert "DWG 77-042" in full_text(data["famB/sheet-3.pdf"]), "rotated-page drawing number lost"


def test_a3_shared_pseudonym(produced):
    """Truth: same identity -> one replacement across in-doc repeats and across documents."""
    _, status, _, data = _outputs(produced)
    if status["famA/drw-001.pdf"] != "transformed":
        pytest.skip("doc1 failed closed (see test_a3_status_transformed)")
    o1 = data["famA/drw-001.pdf"]
    note_val = (line(data["famA/drw-002.pdf"], "NOTES: questions to ") or "")[len("NOTES: questions to "):].strip()
    drawn_val = (line(o1, "DRAWN") or "")[len("DRAWN"):].strip()
    prep_val = (line(o1, "PREPARED BY: ") or "")[len("PREPARED BY:"):].replace(f"AND {CONF}", "").strip()
    assert drawn_val and prep_val and note_val, (drawn_val, prep_val, note_val)
    assert drawn_val not in (P1, CONF)
    assert drawn_val == prep_val == note_val, (drawn_val, prep_val, note_val)


def test_a3_annot_and_meta(produced):
    """Truth: annotation title and metadata author carry the same identity; both must be replaced."""
    _, _, _, data = _outputs(produced)
    t = full_text(data["famA/drw-002.pdf"])
    assert E1 not in t, "original email leaked"
    d2 = open_pdf(data["famA/drw-002.pdf"])
    annot = None
    for p in d2:
        for a in p.annots() or []:
            annot = a.info.get("title")
    author = d2.metadata.get("author")
    d2.close()
    assert annot != P1, annot
    assert author != P1, author
    assert annot == author, (annot, author)


def test_a3_ole_shared_alias(produced):
    """Truth: one shared equal-length account alias across part1, part2 and the zip member."""
    _, status, _, data = _outputs(produced)
    if status["cad/part1.sldprt"] != "transformed":
        pytest.skip("OLE failed (see test_a3_status_transformed)")
    a1, a2 = ole_props(data["cad/part1.sldprt"]), ole_props(data["cad/part2.sldprt"])
    assert a1[0] != ACCOUNT and len(a1[0]) == len(ACCOUNT), a1
    assert a1 == a2, (a1, a2)
    assert ACCOUNT.encode() not in data["cad/part1.sldprt"]
    assert ACCOUNT.encode("utf-16-le") not in data["cad/part1.sldprt"]
    with zipfile.ZipFile(data["bundle/pack.zip"]) as z:
        assert ole_props(z.read("cad/asm.sldasm")) == a1
        assert z.read("readme.txt") == b"plain notes"


def test_a3_rotated_page(produced):
    """Truth: identity on the 90-degree page is replaced with the same pseudonym as everywhere else."""
    _, status, _, data = _outputs(produced)
    if status["famB/sheet-3.pdf"] != "transformed":
        pytest.skip("rotated page failed closed (see test_a3_status_transformed)")
    note_val = (line(data["famA/drw-002.pdf"], "NOTES: questions to ") or "")[len("NOTES: questions to "):].strip()
    appr = (line(data["famB/sheet-3.pdf"], "APPROVED ") or "")[len("APPROVED "):].replace(f"(not {CONF})", "").strip()
    assert appr and appr == note_val, (appr, note_val)
