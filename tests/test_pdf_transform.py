import hashlib

import pymupdf
import pytest

from machina_deid.idmap import IdentityMap, IdentityMapError, from_dict
from machina_deid.pdf_transform import transform_pdf

NAME = "Jane Quincy Doe"


def make_pdf(path, text=True):
    doc = pymupdf.open()
    p = doc.new_page(width=300, height=400)
    if text:
        p.insert_text((50, 100), f"Prepared by {NAME} for review", fontsize=12)
        p.insert_text((50, 140), "Dimension 12.500 mm tolerance 0.05", fontsize=12)
    doc.new_page(width=200, height=250)
    doc.set_metadata({"author": NAME, "title": f"Drawing by {NAME}", "creator": "CAD"})
    doc.embfile_add("note.txt", b"attachment bytes", filename="note.txt")
    doc.save(path)
    doc.close()


def test_redacts_replaces_and_preserves(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "out" / "in.pdf"
    make_pdf(src)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    res = transform_pdf(src, dst, IdentityMap(strings={NAME: "Person-ABCDE"}))
    assert res.ok, res.to_dict()
    assert res.matches == {0: 1} and not res.residual
    assert res.changes.pdf_visible_text_replacements == 1
    assert res.changes.pdf_metadata_identities_changed == 2
    assert res.pages == 2 and res.pages_preserved and res.dimensions_preserved
    assert res.attachments_before == res.attachments_after == 1
    assert res.metadata_cleared and res.xmp_cleared
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before  # source untouched
    out = pymupdf.open(dst)
    text = out[0].get_text()
    assert NAME not in text and "Person-ABCDE" in text
    assert "12.500 mm tolerance 0.05" in text  # numbers untouched
    assert not out.metadata["author"] and not out.metadata["title"]
    assert out.embfile_get("note.txt") == b"attachment bytes"
    assert NAME.encode() not in dst.read_bytes()
    assert not (tmp_path / "out" / "in.pdf.partial").exists()


def test_no_text_reports_and_still_validates_metadata(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "o.pdf"
    make_pdf(src, text=False)
    res = transform_pdf(src, dst, IdentityMap(strings={NAME: "X-1"}))
    assert not res.has_extractable_text and not any(res.matches.values())
    assert any("no extractable text" in w for w in res.warnings)
    assert res.ok and res.metadata_cleared and res.dimensions_preserved


def test_residual_fails_and_no_output_kept(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "o.pdf"
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 100), "Jane Quincy", fontsize=12)
    p.insert_text((50, 130), "Doe", fontsize=12)
    # a mapped string that exists only in outline text cannot be page-redacted
    doc.set_toc([[1, f"{NAME} chapter", 1]])
    doc.save(tmp_path / "t.pdf")
    res = transform_pdf(tmp_path / "t.pdf", dst, IdentityMap(strings={NAME: "P-1"}))
    assert not res.ok and res.residual == {0: 1}
    assert not dst.exists()


def test_refuses_same_path_and_existing_output(tmp_path):
    src = tmp_path / "in.pdf"
    make_pdf(src)
    m = IdentityMap(strings={NAME: "P"})
    with pytest.raises(ValueError):
        transform_pdf(src, src, m, overwrite=True)
    (tmp_path / "o.pdf").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        transform_pdf(src, tmp_path / "o.pdf", m)


def test_idmap_validation_and_repr():
    with pytest.raises(IdentityMapError):
        from_dict({"a": "a"})
    with pytest.raises(IdentityMapError):
        from_dict({"strings": {"a": "b"}, "bogus": {}})
    m = from_dict({"strings": {NAME: "P"}, "layers": {"L": "M"}})
    assert NAME not in repr(m)
