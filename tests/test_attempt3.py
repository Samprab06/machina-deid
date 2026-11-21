"""Attempt 3 regressions: each test failed at 30edc41 (submitted Attempt 2) and passes now. Synthetic only."""
import sys
from pathlib import Path

import pymupdf
import pytest

from machina_deid import ole_transform as ot
from machina_deid import scope as sc
from machina_deid.pdf_scoped import transform_pdf_scoped
from machina_deid.pdf_titleblock import page_lines, scan_page_lines

from ole_helpers import cfb, summary_info

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def _findings(lines_text):
    d = pymupdf.open()
    p = d.new_page()
    for i, t in enumerate(lines_text):
        p.insert_text((40, 60 + 14 * i), t, fontsize=8)
    return {(f.kind, f.value) for f in scan_page_lines(page_lines(p))}


def test_merged_title_block_rows_and_date_tail():
    got = _findings(["APPROVED Ana Duarte", "DRN JONATHAN WHITFIELD 02.03.21", "CHECKED: Maria Keller 12/03/2021"])
    assert ("person", "Ana Duarte") in got
    assert ("person", "JONATHAN WHITFIELD") in got
    assert ("person", "Maria Keller") in got


@pytest.mark.parametrize("text", ["DRAWN TO SCALE", "CHECK VALVE ASSEMBLY", "DESIGN LOAD", "APPROVED 2021-01-01",
                                  "CHECKED PER ECN", "DRAWN STEEL PLATE"])
def test_row_form_confusable_negatives(text):
    assert not {k for k, _ in _findings([text]) if k == "person"}


def _scope(tmp_path, docs):
    items = []
    for rel, data in docs.items():
        p = tmp_path / "in" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        items.append((p, rel))
    secret = b"a3" * 16
    st = sc.build_scope(tmp_path / "m.sqlite", secret, items)
    return sc.Scope(tmp_path / "m.sqlite", secret, st["digest"]), items


def _pdf(lines):
    d = pymupdf.open()
    for page in lines:
        p = d.new_page()
        for i, t in enumerate(page):
            p.insert_text((40, 60 + 14 * i), t, fontsize=8)
    return d.tobytes()


def test_embedded_mention_rewritten_and_trailing_text_kept(tmp_path):
    """MuPDF re-flows text after a mid-string redaction; corrected and verified, not rejected."""
    scope, items = _scope(tmp_path, {"a.pdf": _pdf([["DRAWN BY: Maria Keller"],
                                                    ["Revision notes: updated per Maria Keller markup today"]])})
    res = transform_pdf_scoped(items[0][0], tmp_path / "o.pdf", scope.resolver(), overwrite=True)
    assert res.ok and not res.partial and res.reflow_fixed >= 1
    out = pymupdf.open(tmp_path / "o.pdf")
    t = out[1].get_text()
    assert "Maria Keller" not in t and "markup today" in t
    src = {w[4]: w[:4] for w in pymupdf.open(items[0][0])[1].get_text("words")}
    dst = {w[4]: w[:4] for w in out[1].get_text("words")}
    for w in ("markup", "today"):
        assert abs(src[w][0] - dst[w][0]) < 0.05, (w, src[w][0], dst[w][0])


@pytest.mark.parametrize("rot,origin", [(90, (100, 700)), (270, (100, 100)), (180, (500, 400))])
def test_rotated_text_matrix_reflow_fixed(tmp_path, rot, origin):
    """Mid-string edit in vertical (rotated text-matrix) text: trailing glyphs restored, page not skipped."""
    d = pymupdf.open()
    d.new_page().insert_text(origin, "DRAWN BY: Maria Keller", fontsize=9, rotate=rot)
    d.new_page().insert_text(origin, "Revision notes: updated per Maria Keller markup today", fontsize=9, rotate=rot)
    scope, items = _scope(tmp_path, {"r.pdf": d.tobytes()})
    res = transform_pdf_scoped(items[0][0], tmp_path / "o.pdf", scope.resolver(), overwrite=True)
    assert res.ok and not res.partial and res.skipped_pages == 0 and res.reflow_fixed >= 1
    out = pymupdf.open(tmp_path / "o.pdf")
    assert "Maria Keller" not in out[1].get_text()
    src = {w[4]: w[:4] for w in pymupdf.open(items[0][0])[1].get_text("words")}
    dst = {w[4]: w[:4] for w in out[1].get_text("words")}
    for w in ("markup", "today"):
        assert max(abs(a - b) for a, b in zip(src[w], dst[w])) < 0.05, (w, src[w], dst[w])


def test_canonical_pseudonym_same_in_narrow_and_wide_cells(tmp_path):
    d1 = pymupdf.open()
    p = d1.new_page()
    p.insert_text((40, 60), "DRAWN", fontsize=8)
    p.insert_text((80, 60), "Al Wu", fontsize=8)  # short extent: Attempt 2 switched to the fitted form here
    d2 = _pdf([["CHECKED BY: Al Wu"]])
    scope, items = _scope(tmp_path, {"n.pdf": d1.tobytes(), "w.pdf": d2})
    toks = []
    for src, rel in items:
        r = transform_pdf_scoped(src, tmp_path / f"o-{rel}", scope.resolver(), overwrite=True)
        assert r.ok and r.fitted_used == 0
        toks.append({w[4] for w in pymupdf.open(tmp_path / f"o-{rel}")[0].get_text("words") if "-" in w[4]})
    assert toks[0] and toks[0] == toks[1]


def test_page_failure_keeps_other_pages(tmp_path, monkeypatch):
    from machina_deid import pdf_scoped as ps
    scope, items = _scope(tmp_path, {"a.pdf": _pdf([["DRAWN BY: Maria Keller"], ["CHECKED BY: Maria Keller"]])})
    real_fp, real_once = ps._drawings_fp, ps._transform_once
    state = {"skip": frozenset()}

    def once(*a, **k):
        state["skip"] = k["skip_pages"]
        return real_once(*a, **k)

    def flaky(page, ocr_rects=()):  # simulate a vector change caused by the page-2 edit
        h = real_fp(page, ocr_rects)
        edited = page.number == 1 and 1 not in state["skip"] and page.parent.name.endswith(".partial")
        return h + "x" if edited else h
    monkeypatch.setattr(ps, "_drawings_fp", flaky)
    monkeypatch.setattr(ps, "_transform_once", once)
    res = transform_pdf_scoped(items[0][0], tmp_path / "o.pdf", scope.resolver(), overwrite=True)
    assert res.ok and res.partial and res.skipped_pages == 1
    out = pymupdf.open(tmp_path / "o.pdf")
    assert "Maria Keller" not in out[0].get_text() and "Maria Keller" in out[1].get_text()


def test_ole_author_and_length_prefixed_records(tmp_path):
    si = summary_info({4: (0x1E, "tlindqvist"), 8: (0x1E, "mkeller")})
    import struct
    contents = (b"\0" * 32 + struct.pack("<I", 10) + "tlindqvist".encode("utf-16-le") + b"\0\0"
                + b"\x01" * 8 + struct.pack("<I", 8) + b"mkeller\0" + b"\x01" * 8
                + "xmkellerx".encode("utf-16-le") + b"\x01" * 16)  # embedded in a longer word: not touched
    data = cfb({"\x05SummaryInformation": si, "Contents": contents})
    scope, items = _scope(tmp_path, {"p.sldprt": data})
    r = scope.ole(data)
    assert r.status == "transformed" and r.property_writes == 2 and r.cross_stream_writes == 2
    for v in ("tlindqvist", "mkeller"):
        assert v.encode("utf-16-le") not in r.data.replace("xmkellerx".encode("utf-16-le"), b"")
    assert b"mkeller\0" not in r.data and "xmkellerx".encode("utf-16-le") in r.data
    assert len(r.data) == len(data)


def test_synthetic_end_to_end_targets(tmp_path):
    import attempt3_e2e as h
    res = h.run(tmp_path)
    assert res["pdf_removed"] == res["pdf_occurrences"]
    assert res["pdf_pairs_consistent"] == res["pdf_pairs"]
    assert res["triplets_ok"] == res["triplets"]
    assert res["ole_removed"] == res["ole_values"]


def test_same_identity_twice_with_hyphenated_confusable_is_clean(tmp_path):
    """RCAC Stage-0 shape (own values): DRAWN cell + inline PREPARED BY of one identity, a different
    hyphenated name on the same line, metadata author; Attempt 2 failed closed (resolved_value_residual)."""
    from machina_deid import twopass
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((40, 60), "DRAWN", fontsize=8)
    p.insert_text((80, 60), "Ana Duarte", fontsize=8)
    p.insert_text((40, 80), "PREPARED BY Ana Duarte with notes from Ana Duarte-Lopes", fontsize=8)
    p.insert_text((40, 100), "DWG 77-042   SCALE 1:2   Ø120 ±0.05", fontsize=8)
    d.set_metadata({"author": "Ana Duarte"})
    scope, items = _scope(tmp_path, {"f/doc1.pdf": d.tobytes()})

    class Cat:
        def approved_pseudonym(self, v):
            return None
    r = twopass.transform_file(Cat(), items[0][0], tmp_path / "o.pdf", "f/doc1.pdf", overwrite=True, scope=scope)
    assert r.status == "transformed" and r.fully_clean, (r.reason, r.independent_residual)
    o = pymupdf.open(tmp_path / "o.pdf")
    t = o[0].get_text()
    assert "Ana Duarte-Lopes" in t and t.count("Ana Duarte") == 1
    assert "DWG 77-042   SCALE 1:2   Ø120 ±0.05" in t
    ps = o.metadata["author"]
    assert ps != "Ana Duarte" and t.count(ps) == 2


def test_ole_preview_stream_written_unless_real_image():
    """Account inside a path in a non-container preview stream is aliased; a real PNG preview is never written."""
    from machina_deid import ole_transform as ot
    from ole_helpers import cfb, summary_info
    acc, alias = "jqdoe7", "zz81kq"
    path = ("C:\\Users\\" + acc + "\\x").encode("utf-16-le")
    for head, written in ((b"\x89PNG preview bytes ", True), (b"\x89PNG\r\n\x1a\n", False)):
        src = cfb({"\x05SummaryInformation": summary_info({8: (0x1E, acc)}), "PreviewPNG": head + path})
        res = ot.transform(src, {acc: alias})
        assert res.reason is None
        assert (acc.encode("utf-16-le") in res.data) is (not written)
        assert len(res.data) == len(src)
