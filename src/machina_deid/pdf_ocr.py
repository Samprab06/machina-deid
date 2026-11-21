"""Attempt 2c: OCR for textless PDF pages (raster scans or text drawn as vector outlines).

Uses PyMuPDF's Tesseract integration (no extra Python dependency). Languages default to the ones installed in
the local WSL (``eng+deu+fra``; override with ``MACHINA_OCR_LANGS``). OCR output is converted to the same
:class:`pdf_titleblock.Line` objects the scanner uses, so role labels / contact patterns / lookups apply
unchanged. ``ENGINE`` is a module-level hook so tests can substitute a deterministic engine.
"""
from __future__ import annotations

import os
from functools import lru_cache

import pymupdf

from .pdf_titleblock import Line, page_lines

DPI = int(os.environ.get("MACHINA_OCR_DPI", "200"))


def langs() -> str:
    return os.environ.get("MACHINA_OCR_LANGS", "eng+deu+fra")


@lru_cache(maxsize=1)
def available() -> bool:
    try:
        return bool(pymupdf.get_tessdata())
    except Exception:  # noqa: BLE001 - tesseract/tessdata absent
        return False


def is_textless(page) -> bool:
    return not page_lines(page)


def _tesseract_lines(page, clip=None) -> list[Line]:
    """OCR ``page`` (optionally only ``clip``) into character lines in page (unrotated text) space."""
    if not available():
        raise RuntimeError("ocr_unavailable")
    tp = page.get_textpage_ocr(language=langs(), dpi=DPI, full=True)
    lines = []
    for ln in _rawdict_lines(page, tp):
        if clip is None or pymupdf.Rect(ln.bbox).intersects(clip):
            lines.append(ln)
    return lines


def _rawdict_lines(page, tp) -> list[Line]:
    raw = page.get_text("rawdict", textpage=tp)
    out: list[Line] = []
    for b in raw.get("blocks", []):
        for ln in b.get("lines", []):
            chars, boxes, origins, size = [], [], [], 0.0
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    if ch.get("c"):
                        size = size or float(sp.get("size", 0))
                        chars.append(ch["c"])
                        boxes.append(tuple(ch["bbox"]))
                        origins.append(tuple(ch.get("origin", ch["bbox"][:2])))
            if chars and "".join(chars).strip():
                out.append(Line(page.number, "".join(chars), boxes, tuple(ln.get("dir", (1.0, 0.0))), size, 0,
                                "ocr", origins[0], origins))
    return out


ENGINE = _tesseract_lines  # (page, clip=None) -> list[Line]


def ocr_page_lines(page, clip=None) -> list[Line]:
    return ENGINE(page, clip)


def region_text(page, rect) -> str:
    """Re-OCR text read inside ``rect`` (used to verify an original is no longer legible)."""
    r = pymupdf.Rect(rect) + (-1, -1, 1, 1)
    return "".join(c for ln in ocr_page_lines(page, r) for c, b in zip(ln.text, ln.boxes)
                   if r.contains(pymupdf.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)))
