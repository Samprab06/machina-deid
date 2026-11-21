"""Attempt 2c OCR coverage of textless PDF pages (stub engine here; real Tesseract when installed)."""
import pymupdf
import pytest

from machina_deid import pdf_ocr
from machina_deid import scope as sc
from machina_deid.pdf_entities import Resolver
from machina_deid.pdf_scoped import transform_pdf_scoped
from machina_deid.pdf_titleblock import Line

NAME = "Bob Builder"
LABEL_RECT = (520, 490, 560, 502)
NAME_RECT = (580, 490, 650, 502)


def _image_bytes(text_items):
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    for (x, y), t, fs in text_items:
        p.insert_text((x, y), t, fontsize=fs)
    pix = p.get_pixmap(dpi=150)
    d.close()
    return pix.tobytes("png")


def textless_pdf(path, name=NAME):
    """One page: a raster scan of a title block + real vector geometry (no extractable text)."""
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    p.insert_image(p.rect, stream=_image_bytes([((520, 500), "DRAWN", 9), ((580, 500), name, 9),
                                                ((100, 100), "SECTION A-A", 9)]))
    p.draw_rect(pymupdf.Rect(510, 480, 830, 585), color=(0, 0, 0), width=0.8)
    p.draw_circle((200, 250), 60, color=(0, 0, 0))
    d.save(path)
    d.close()
    return path


def _boxes(rect, text):
    x0, y0, x1, y1 = rect
    w = (x1 - x0) / len(text)
    return [(x0 + i * w, y0, x0 + (i + 1) * w, y1) for i in range(len(text))]


def _line(page, rect, text):
    b = _boxes(rect, text)
    return Line(page.number, text, b, (1.0, 0.0), 9.0, 0, "ocr", (rect[0], rect[3] - 2),
                [(x[0], rect[3] - 2) for x in b])


def stub_engine(original_digest_holder, always=False):
    def engine(page, clip=None):
        digests = {i.get("digest") for i in page.get_image_info(hashes=True)}
        if not always and original_digest_holder[0] not in digests:
            return []  # pixels changed: the stub no longer "reads" the original title block words
        out = [_line(page, LABEL_RECT, "DRAWN"), _line(page, NAME_RECT, NAME)]
        return [ln for ln in out if clip is None or pymupdf.Rect(ln.bbox).intersects(clip)]
    return engine


@pytest.fixture
def ocr_env(tmp_path, monkeypatch):
    src = textless_pdf(tmp_path / "scan.pdf")
    d = pymupdf.open(src)
    holder = [d[0].get_image_info(hashes=True)[0]["digest"]]
    d.close()
    monkeypatch.setattr(pdf_ocr, "ENGINE", stub_engine(holder))
    secret = b"o" * 32
    st = sc.build_scope(tmp_path / "m.sqlite", secret, [(src, "scans/scan.pdf")], ocr=True)
    return src, secret, st, holder, monkeypatch


def test_scope_build_reads_textless_pages_only_with_ocr(ocr_env, tmp_path):
    src, secret, st, *_ = ocr_env
    assert st.get("kind:person") == 1
    st0 = sc.build_scope(tmp_path / "m0.sqlite", secret, [(src, "scans/scan.pdf")], ocr=False)
    assert st0["entities"] == 0


def test_ocr_rewrite_verified_and_geometry_preserved(ocr_env, tmp_path):
    src, secret, st, *_ = ocr_env
    r = Resolver(secret, tmp_path / "m.sqlite", expect_digest=st["digest"])
    res = transform_pdf_scoped(src, tmp_path / "out.pdf", r, ocr=True)
    assert res.ok, res.reason
    assert res.ocr_pages == 1 and res.ocr_edits_verified == 1 and res.edits_verified == 1
    o, i = pymupdf.open(tmp_path / "out.pdf"), pymupdf.open(src)
    try:
        pseudo = r.entry(NAME).pseudonym
        txt = o[0].get_text()
        assert NAME not in txt and (pseudo in txt or r.entry(NAME).fitted in txt)
        assert len(o[0].get_drawings()) == len(i[0].get_drawings())  # vector geometry untouched
        assert o[0].get_image_info()[0]["bbox"] == i[0].get_image_info()[0]["bbox"]
    finally:
        o.close()
        i.close()


def test_ocr_rewrite_refused_when_original_still_legible(ocr_env, tmp_path):
    src, secret, st, holder, monkeypatch = ocr_env
    monkeypatch.setattr(pdf_ocr, "ENGINE", stub_engine(holder, always=True))
    r = Resolver(secret, tmp_path / "m.sqlite", expect_digest=st["digest"])
    res = transform_pdf_scoped(src, tmp_path / "out.pdf", r, ocr=True)
    assert not res.ok and res.reason == "edit_verification_failed" and not (tmp_path / "out.pdf").exists()


def test_ocr_off_leaves_textless_page(ocr_env, tmp_path):
    src, secret, st, *_ = ocr_env
    r = Resolver(secret, tmp_path / "m.sqlite", expect_digest=st["digest"])
    res = transform_pdf_scoped(src, tmp_path / "out.pdf", r, ocr=False)
    assert not res.ok and res.reason == "no_resolved_identities" and res.textless_pages == 1


@pytest.mark.skipif(not pdf_ocr.available(), reason="Tesseract/tessdata not installed")
def test_real_tesseract_roundtrip(tmp_path):
    src = tmp_path / "scan.pdf"
    d = pymupdf.open()
    p = d.new_page(width=842, height=595)
    p.insert_image(p.rect, stream=_image_bytes([((420, 300), "DRAWN", 20), ((520, 300), NAME, 20)]))
    d.save(src)
    d.close()
    secret = b"t" * 32
    st = sc.build_scope(tmp_path / "m.sqlite", secret, [(src, "scan.pdf")], ocr=True)
    assert st["entities"] >= 1, st
    r = Resolver(secret, tmp_path / "m.sqlite", expect_digest=st["digest"])
    res = transform_pdf_scoped(src, tmp_path / "out.pdf", r, ocr=True)
    assert res.ok, (res.reason, res.to_dict())
