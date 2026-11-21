"""Guarded local OCR of textless PDF pages (attempt 2c).

Textless pages (scans, outlined/vectorised text) are rendered and read with the local Tesseract binary
(``eng+deu+fra`` by default) so that title-block discovery (:func:`pdf_titleblock.scan_page_lines`) runs on
them exactly as on text pages. Recovered words become :class:`OcrLine` objects in *unrotated page space*, so
the planner, the rewrite and the verifier use the same geometry as for extracted text.

Guards (each refusal is counted with a value-free code in ``stats``):

* only pages with no extractable text, with an image or vector content, and bounded rendered size;
* at most ``max_pages`` OCR pages per document, bounded per-call timeout;
* the page is read in the orientation (0/90/180/270 deg) with the best confident-word score, so rotated
  scans and sideways title blocks work; the orientation is kept per page for re-OCR verification;
* only words at or above ``min_conf`` count toward a finding (enforced by the planner per span).

Privacy: images go to Tesseract over stdin and text comes back over stdout. Nothing is written to disk,
stderr is discarded, and OCR text is never logged or put in exceptions. Only counts leave this module.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Protocol

import pymupdf

from .pdf_titleblock import Line, match_label

DEFAULT_LANGS = "eng+deu+fra"


@dataclass
class Word:
    text: str
    conf: float
    box: tuple[float, float, float, float]  # left, top, right, bottom in (rotated) image pixels
    line_key: tuple = ()


@dataclass
class OcrLine(Line):
    """A line recovered by OCR; ``confs`` is per character (spaces carry the lower neighbour's confidence)."""
    confs: list[float] = field(default_factory=list, repr=False)
    rotation: int = 0  # degrees the rendered page image was rotated (counter-clockwise) to read it upright


class Engine(Protocol):
    available: bool
    reason: str

    def words(self, png: bytes, psm: int) -> list[Word]: ...


@dataclass
class OcrConfig:
    langs: str = DEFAULT_LANGS
    dpi: int = 300
    min_dpi: int = 150
    max_pixels: int = 60_000_000
    min_conf: float = 70.0
    timeout: float = 90.0
    max_pages: int = 4
    psm: int = 11  # sparse text: title blocks are scattered cells, not paragraphs


class TesseractEngine:
    """Local ``tesseract`` CLI. ``available`` is False (with a code in ``reason``) when the binary or one of
    the requested language packs is missing; callers then skip OCR and count ``ocr_unavailable``."""

    def __init__(self, cfg: OcrConfig | None = None, exe: str | None = None):
        self.cfg = cfg or OcrConfig()
        self.exe = exe or shutil.which("tesseract")
        self.available, self.reason = False, "tesseract_missing"
        if not self.exe:
            return
        try:
            out = subprocess.run([self.exe, "--list-langs"], capture_output=True, timeout=30, check=False)
            have = set(out.stdout.decode("utf-8", "ignore").split())
        except (OSError, subprocess.SubprocessError):
            self.reason = "tesseract_error"
            return
        missing = [lang for lang in self.cfg.langs.split("+") if lang not in have]
        if missing:
            self.reason = "language_pack_missing"
            return
        self.available, self.reason = True, ""

    def words(self, png: bytes, psm: int) -> list[Word]:
        try:
            out = subprocess.run([self.exe, "stdin", "stdout", "-l", self.cfg.langs, "--psm", str(psm), "tsv"],
                                 input=png, capture_output=True, timeout=self.cfg.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OcrError("timeout") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise OcrError("exec_error") from exc
        if out.returncode != 0:
            raise OcrError("nonzero_exit")
        return parse_tsv(out.stdout.decode("utf-8", "replace"))


class OcrError(Exception):
    """Value-free OCR failure code."""


def parse_tsv(tsv: str) -> list[Word]:
    words = []
    rows = tsv.splitlines()
    for row in rows[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        try:
            left, top, w, h, conf = int(f[6]), int(f[7]), int(f[8]), int(f[9]), float(f[10])
        except ValueError:
            continue
        words.append(Word(f[11].strip(), conf, (left, top, left + w, top + h), (f[1], f[2], f[3], f[4])))
    return words


# ------------------------------------------------------------------ geometry


def _rotate_png(pix: pymupdf.Pixmap, deg: int) -> tuple[bytes, int, int]:
    """PNG bytes of the pixmap rotated counter-clockwise by ``deg`` (0/90/180/270); (png, width, height)."""
    if deg == 0:
        return pix.tobytes("png"), pix.width, pix.height
    from PIL import Image
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    img = img.transpose({90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
                         270: Image.Transpose.ROTATE_270}[deg])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), img.width, img.height


def _unrotate_pt(x: float, y: float, deg: int, w: int, h: int) -> tuple[float, float]:
    """Point in the image rotated CCW by ``deg`` (size of the *original* image: w x h) -> original pixels."""
    if deg == 0:
        return x, y
    if deg == 90:  # rotated image is h x w; original (x0, y0) -> (y0, w - x0)
        return w - y, x
    if deg == 180:
        return w - x, h - y
    return y, h - x  # 270


def words_to_lines(words: list[Word], deg: int, img_w: int, img_h: int, zoom: float, page,
                   pno: int) -> list[OcrLine]:
    """Group words by Tesseract line and map them into unrotated page space (per-character boxes)."""
    to_page = pymupdf.Matrix(1 / zoom, 1 / zoom) * page.derotation_matrix

    def pt(x, y):
        ox, oy = _unrotate_pt(x, y, deg, img_w, img_h)
        return pymupdf.Point(ox, oy) * to_page

    groups: dict[tuple, list[Word]] = {}
    for w in words:
        groups.setdefault(w.line_key, []).append(w)
    out = []
    for ws in groups.values():
        ws.sort(key=lambda w: w.box[0])
        text, boxes, origins, confs = [], [], [], []
        hpx = max(w.box[3] - w.box[1] for w in ws)
        for i, w in enumerate(ws):
            if i:
                prev = ws[i - 1]
                text.append(" ")
                gx0, gx1 = prev.box[2], max(prev.box[2] + 1, w.box[0])
                boxes.append(_quad_rect(pt, gx0, w.box[1], gx1, w.box[3]))
                origins.append(tuple(pt(gx0, w.box[3])))
                confs.append(min(prev.conf, w.conf))
            n = len(w.text)
            cw = (w.box[2] - w.box[0]) / max(n, 1)
            for k, ch in enumerate(w.text):
                x0 = w.box[0] + k * cw
                text.append(ch)
                boxes.append(_quad_rect(pt, x0, w.box[1], x0 + cw, w.box[3]))
                origins.append(tuple(pt(x0, w.box[3])))
                confs.append(w.conf)
        a, b = pt(ws[0].box[0], ws[0].box[3]), pt(ws[0].box[0] + 100, ws[0].box[3])
        dx, dy = b.x - a.x, b.y - a.y
        norm = (dx * dx + dy * dy) ** 0.5 or 1.0
        size = hpx / zoom * 0.8
        out.append(OcrLine(pno, "".join(text), boxes, (dx / norm, dy / norm), size, 0, "ocr", origins[0],
                           origins, confs=confs, rotation=deg))
    return out


def _quad_rect(pt: Callable, x0, y0, x1, y1) -> tuple[float, float, float, float]:
    ps = [pt(x0, y0), pt(x1, y0), pt(x0, y1), pt(x1, y1)]
    xs, ys = [p.x for p in ps], [p.y for p in ps]
    return min(xs), min(ys), max(xs), max(ys)


# ------------------------------------------------------------------ page OCR


_ALPHA2 = re.compile(r"[^\W\d_]{2,}")


def _score(words: list[Word], min_conf: float) -> float:
    good = [w for w in words if w.conf >= min_conf and _ALPHA2.search(w.text)]
    return len(good) + 5 * sum(bool(match_label(w.text)) for w in good)


def _content_present(page) -> bool:
    try:
        if page.get_image_info():
            return True
        return len(page.get_drawings()) > 20
    except Exception:  # noqa: BLE001
        return False


def ocr_page_lines(page, engine: Engine, stats, cfg: OcrConfig | None = None) -> list[OcrLine]:
    """OCR one textless page (guards apply). Returns lines in unrotated page space ([] when refused)."""
    cfg = cfg or getattr(engine, "cfg", None) or OcrConfig()
    if not engine.available:
        stats[f"skipped:{engine.reason or 'unavailable'}"] += 1
        return []
    if stats["pages_ocr"] >= cfg.max_pages:
        stats["skipped:page_budget"] += 1
        return []
    if not _content_present(page):
        stats["skipped:blank"] += 1
        return []
    dpi = cfg.dpi
    area_in2 = abs(page.rect) / (72 * 72)
    while dpi > cfg.min_dpi and area_in2 * dpi * dpi > cfg.max_pixels:
        dpi = int(dpi * 0.8)
    if area_in2 * dpi * dpi > cfg.max_pixels:
        stats["skipped:too_large"] += 1
        return []
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    stats["pages_ocr"] += 1
    best = None
    try:
        for deg in (0, 90, 270, 180):
            png, _, _ = _rotate_png(pix, deg)
            words = engine.words(png, cfg.psm)
            sc = _score(words, cfg.min_conf)
            if best is None or sc > best[0]:
                best = (sc, deg, words)
            if deg == 0 and sc >= 25:
                break  # confidently upright: skip the other orientations
    except OcrError as exc:
        stats[f"error:{exc}"] += 1
        return []
    if not best or best[0] <= 0:
        stats["no_confident_text"] += 1
        return []
    stats[f"orientation:{best[1]}"] += 1
    stats["pages_text_recovered"] += 1
    return words_to_lines(best[2], best[1], pix.width, pix.height, zoom, page, page.number)


def region_text(page, rect: pymupdf.Rect, deg: int, engine: Engine, dpi: int = 300) -> str | None:
    """Re-OCR one (unrotated-space) rect of a page in the orientation it was read in; None if unavailable.
    The returned text stays in memory (used only for a containment check)."""
    if not engine.available:
        return None
    clip = (pymupdf.Rect(rect) * page.rotation_matrix) & page.rect
    if clip.is_empty:
        return ""
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip + (-2, -2, 2, 2), colorspace=pymupdf.csGRAY,
                          alpha=False)
    png, _, _ = _rotate_png(pix, deg)
    try:
        return " ".join(w.text for w in engine.words(png, 7))
    except OcrError:
        return None
