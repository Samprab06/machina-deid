"""Phase 5F: independent read-only PDF residual inspector (real PyMuPDF-generated PDFs)."""
import hashlib
import json

import pymupdf
import pytest

from machina_deid import twopass as tp
from machina_deid.catalog import IdentityCatalog
from machina_deid.pdf_residual import CATEGORIES, inspect_pdf

ORG = "Acme Widgets Inc"
AMP = "Smith & Jones LLC"
VALUES = [ORG, AMP]


def _save(doc, path):
    doc.save(path)
    doc.close()
    return path


def _new(w=300, h=400):
    doc = pymupdf.open()
    doc.new_page(width=w, height=h)
    return doc


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _only(rep, cat):
    assert rep.ok, rep.reason
    assert rep.residual[cat] >= 1, rep.to_dict()
    others = {k: v for k, v in rep.residual.items() if k != cat and v}
    return others


def _check_no_raw(rep):
    blob = json.dumps(rep.to_dict()).casefold()
    for v in VALUES:
        assert v.casefold() not in blob


def test_clean_pdf_zero_residual(tmp_path):
    doc = _new()
    doc[0].insert_text((50, 100), "Bracket drawing rev B")
    p = _save(doc, tmp_path / "c.pdf")
    before = _sha(p)
    rep = inspect_pdf(p, VALUES)
    assert rep.ok and rep.clean and rep.total == 0 and rep.pages == 1
    assert rep.dimensions == [(300.0, 400.0, 300.0, 400.0, 0)]
    assert set(rep.residual) == set(CATEGORIES)
    assert _sha(p) == before  # read-only


def test_info_metadata_standard_and_custom_keys(tmp_path):
    doc = _new()
    doc.set_metadata({"title": "Drawing"})
    info = int(doc.xref_get_key(-1, "Info")[1].split()[0])
    doc.xref_set_key(info, "Company", pymupdf.get_pdf_str(ORG))
    p = _save(doc, tmp_path / "m.pdf")
    rep = inspect_pdf(p, VALUES)
    assert not _only(rep, "info_metadata")
    _check_no_raw(rep)


def test_xmp_metadata_escaped(tmp_path):
    doc = _new()
    doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><dc:creator>Smith &amp; Jones LLC'
                         '</dc:creator></x:xmpmeta>')
    p = _save(doc, tmp_path / "x.pdf")
    rep = inspect_pdf(p, VALUES)
    assert not _only(rep, "xmp_metadata")
    assert rep.residual_by_index == {1: rep.total}
    _check_no_raw(rep)


def test_annotation_content(tmp_path):
    doc = _new()
    a = doc[0].add_text_annot((20, 20), f"Reviewed by {ORG}")
    a.set_info(title="reviewer")
    a.update()
    p = _save(doc, tmp_path / "a.pdf")
    assert not _only(inspect_pdf(p, VALUES), "annotations")


def test_form_field_value(tmp_path):
    doc = _new()
    w = pymupdf.Widget()
    w.field_name, w.field_type, w.field_value = "customer", pymupdf.PDF_WIDGET_TYPE_TEXT, ORG
    w.rect = pymupdf.Rect(50, 300, 250, 320)
    doc[0].add_widget(w)
    p = _save(doc, tmp_path / "f.pdf")
    rep = inspect_pdf(p, VALUES)
    assert rep.residual["form_fields"] >= 1, rep.to_dict()
    _check_no_raw(rep)


def test_embedded_file_and_attachment_annot(tmp_path):
    doc = _new()
    doc.embfile_add("notes.txt", f"owner: {ORG}".encode(), filename="notes.txt", desc="internal")
    doc[0].add_file_annot((30, 30), b"plain bytes", "a.txt", desc=f"from {ORG}")
    p = _save(doc, tmp_path / "e.pdf")
    rep = inspect_pdf(p, VALUES)
    assert rep.residual["embedded_files"] >= 2, rep.to_dict()
    assert rep.inspected["embedded_files"] >= 2


def test_outline_title(tmp_path):
    doc = _new()
    doc.set_toc([[1, f"Prepared for {ORG}", 1]])
    p = _save(doc, tmp_path / "o.pdf")
    assert not _only(inspect_pdf(p, VALUES), "outlines")


def test_link_uri_url_encoded(tmp_path):
    doc = _new()
    doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 60, 30),
                        "uri": "https://example.invalid/c?n=Acme%20Widgets%20Inc"})
    p = _save(doc, tmp_path / "l.pdf")
    assert not _only(inspect_pdf(p, VALUES), "links")


def test_rotated_text(tmp_path):
    doc = _new()
    doc[0].insert_text((100, 300), ORG, rotate=90, fontsize=11)
    p = _save(doc, tmp_path / "r.pdf")
    assert not _only(inspect_pdf(p, VALUES), "visible_text")


def test_rotated_page(tmp_path):
    doc = _new()
    doc[0].insert_text((50, 100), ORG)
    doc[0].set_rotation(90)
    p = _save(doc, tmp_path / "rp.pdf")
    rep = inspect_pdf(p, VALUES)
    assert rep.residual["visible_text"] >= 1 and rep.dimensions[0][4] == 90


def test_split_spans_and_lines(tmp_path):
    doc = _new()
    pg = doc[0]
    pg.insert_text((50, 100), "Acme Wid", fontname="helv", fontsize=12)
    x = 50 + pymupdf.get_text_length("Acme Wid", fontname="helv", fontsize=12)
    pg.insert_text((x, 100), "gets Inc", fontname="cour", fontsize=12)  # separate span, different font
    pg.insert_text((50, 200), "Acme Widgets", fontsize=12)  # split across lines
    pg.insert_text((50, 215), "Inc", fontsize=12)
    p = _save(doc, tmp_path / "s.pdf")
    rep = inspect_pdf(p, VALUES)
    assert rep.residual["visible_text"] >= 2, rep.to_dict()


def test_unreadable_is_not_clean(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4 garbage")
    rep = inspect_pdf(p, VALUES)
    assert not rep.ok and not rep.clean


# ---------------------------------------------------------------- twopass integration

SECRET = b"unit-test-secret-0123456789"


@pytest.fixture
def cat(tmp_path):
    c = IdentityCatalog(tmp_path / "cat" / "cat.sqlite", SECRET)
    c.approve("org", ORG, reason="explicit test approval")
    yield c
    c.close()


def _pdf(path, extra=None):
    doc = pymupdf.open()
    doc.new_page(width=300, height=400).insert_text((50, 100), f"Prepared for {ORG}", fontsize=12)
    doc.new_page(width=612, height=792)
    doc.set_metadata({"author": ORG})
    if extra:
        extra(doc)
    return _save(doc, path)


def test_twopass_clean_pdf_fully_clean_geometry_preserved(tmp_path, cat):
    src = _pdf(tmp_path / "a.pdf")
    before = _sha(src)
    dst = tmp_path / "out" / "a.pdf"
    o = tp.transform_file(cat, src, dst)
    assert o.status == "transformed" and o.fully_clean and o.independent_residual == {}, o.to_dict()
    assert _sha(src) == before and o.source_unchanged
    s, d = inspect_pdf(src, []), inspect_pdf(dst, [ORG])
    assert (s.pages, s.dimensions) == (d.pages, d.dimensions) and d.clean


@pytest.mark.parametrize("extra,cat_name", [
    (lambda d: d.embfile_add("n.txt", ORG.encode(), desc="x"), "embedded_files"),
    (lambda d: d[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(1, 1, 9, 9),
                                 "uri": "https://example.invalid/Acme%20Widgets%20Inc"}), "links"),
])
def test_twopass_independent_residual_fails_and_removes_output(tmp_path, cat, extra, cat_name):
    src = _pdf(tmp_path / "a.pdf", extra)
    before = _sha(src)
    dst = tmp_path / "out" / "a.pdf"
    o = tp.transform_file(cat, src, dst)
    assert o.status == "failed" and not o.fully_clean and not o.output_written and not dst.exists(), o.to_dict()
    assert o.independent_residual.get(cat_name, 0) >= 1 and o.residual_approved >= 1
    assert ORG not in json.dumps(o.to_dict())
    assert _sha(src) == before and o.source_unchanged


def test_twopass_geometry_mismatch_fails(tmp_path, cat, monkeypatch):
    from machina_deid import pdf_residual
    src = _pdf(tmp_path / "a.pdf")
    real = pdf_residual.inspect_pdf

    def fake(path, values):
        r = real(path, values)
        if values:  # the output scan
            r.dimensions = [(1.0, 1.0, 1.0, 1.0, 0)] * r.pages
        return r
    monkeypatch.setattr(pdf_residual, "inspect_pdf", fake)
    o = tp.transform_file(cat, src, tmp_path / "out.pdf")
    assert o.status == "failed" and o.reason == "independent_geometry_mismatch" and not o.fully_clean


def test_zip_member_pdf_independent_residual(tmp_path, cat):
    import zipfile
    inner = _pdf(tmp_path / "m.pdf", lambda d: d.embfile_add("n.txt", ORG.encode()))
    src = tmp_path / "a.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.write(inner, "docs/m.pdf")
    o = tp.transform_file(cat, src, tmp_path / "out.zip")
    assert o.residual_approved >= 1 and not o.fully_clean, o.to_dict()
