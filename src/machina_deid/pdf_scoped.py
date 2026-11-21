"""Attempt 2c: scoped, verified PDF identity rewrite.

Per page: discover (role-labelled title-block cells, contact patterns, frozen-map lookups of unlabelled text),
resolve each value through :class:`pdf_entities.Resolver`, then rewrite exactly the character extent of each
occurrence: a text-only redaction (images and vector graphics untouched) followed by the pseudonym drawn at
the original baseline origin, direction, colour and (fitted) size. Metadata, XMP, annotations, form fields,
bookmarks and attachment descriptors get exact string substitution of the document's resolved values.

Every edit is independently verified on the re-opened output:

* the original value is absent from the edit region and the replacement is extractable there;
* the replacement renders (ink present in the region's pixmap);
* every character outside all edit regions is preserved (same glyph at the same position);
* vector drawings, images, page count/boxes/rotation and link targets are unchanged.

Any failed check discards the output (never a fallback copy claimed as transformed).
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .identity import _normalize
from .pdf_entities import Resolver
from .pdf_titleblock import Finding, Line, lookup_findings, page_lines, scan_page_lines

FONT = "helv"
MIN_SCALE = 0.55  # replacement font may shrink to 55% of the original size before the fitted form is used
_INFO_KEYS = ("title", "author", "subject", "keywords", "creator", "producer")


@dataclass
class Edit:
    page: int
    kind: str
    source: str
    old: str = field(repr=False)
    new: str = field(repr=False)
    rect: tuple[float, float, float, float]
    dir: tuple[float, float]
    origin: tuple[float, float]
    size: float
    color: int
    fitted: bool = False
    fontsize: float = 0.0
    status: str = "planned"  # planned | verified | failed:<reason>
    ocr: bool = False  # planned from OCR of a textless page: redact pixels / covered outlines, verify by re-OCR


@dataclass
class ScopedResult:
    ok: bool = False
    reason: str = ""
    pages: int = 0
    findings: Counter = field(default_factory=Counter)  # source/kind -> n
    unresolved: Counter = field(default_factory=Counter)  # reason -> n
    edits_planned: int = 0
    edits_verified: int = 0
    fitted_used: int = 0
    metadata_replacements: int = 0
    annotation_replacements: int = 0
    widget_replacements: int = 0
    toc_replacements: int = 0
    attachment_replacements: int = 0
    residual: Counter = field(default_factory=Counter)  # location -> n (resolved values left)
    textless_pages: int = 0
    ocr_pages: int = 0
    ocr_edits_verified: int = 0
    trace: list[dict] = field(default_factory=list)  # value-free per-edit records (page, rect, kind, status)
    resolved_values: list[str] = field(default_factory=list, repr=False)  # raw, memory only

    @property
    def changes(self) -> int:
        return (self.edits_verified + self.metadata_replacements + self.annotation_replacements
                + self.widget_replacements + self.toc_replacements + self.attachment_replacements)

    def to_dict(self) -> dict:
        d = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in self.__dict__.items()}
        d.pop("resolved_values")
        d["changes"] = self.changes
        return d


# ------------------------------------------------------------------ planning


def _rgb(color: int) -> tuple[float, float, float]:
    return ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255


def _extent(line: Line, a: int, b: int) -> float:
    c, s = line.dir
    us = []
    for x0, y0, x1, y1 in line.boxes[a:b]:
        us += [x0 * c + y0 * s, x1 * c + y1 * s, x0 * c + y1 * s, x1 * c + y0 * s]
    return max(us) - min(us)


def _choose_text(new: str, fit: str, avail: float, size: float) -> tuple[str, float, bool]:
    size = size if size > 0 else 8.0
    for text, is_fit in ((new, False), (fit, True)):
        w1 = pymupdf.get_text_length(text, fontname=FONT, fontsize=1.0) or 1.0
        fs = min(size, avail / w1)
        if fs >= size * MIN_SCALE:
            return text, fs, is_fit
    w1 = pymupdf.get_text_length(fit, fontname=FONT, fontsize=1.0) or 1.0
    return fit, max(size * MIN_SCALE, min(size, avail / w1)), True  # may overflow the extent; still readable


def plan_page(page, resolver: Resolver, res: ScopedResult, lines=None, ocr: bool = False) -> list[Edit]:
    lines = page_lines(page) if lines is None else lines
    found: list[Finding] = scan_page_lines(lines)
    for f in found:
        res.findings[f"{f.source}:{f.kind}"] += 1
    edits: list[Edit] = []

    def add(f: Finding, entry) -> None:
        ln = f.line
        r = f.rect
        for e in edits:
            if pymupdf.Rect(e.rect).intersects(r) and abs(pymupdf.Rect(e.rect) & r) > 0.25 * abs(r):
                return
        new, fs, is_fit = _choose_text(entry.pseudonym, entry.fitted, _extent(ln, f.start, f.end), ln.size)
        org = ln.origins[f.start] if ln.origins else ln.origin
        edits.append(Edit(page.number, f.kind, f.source, f.value, new, tuple(r), ln.dir, tuple(org), ln.size,
                          ln.color, is_fit, fs, ocr=ocr))

    for f in sorted(found, key=lambda f: -(f.end - f.start)):
        entry, why = resolver.resolve(f.kind, f.value)
        if entry is None:
            res.unresolved[why] += 1
            res.trace.append({"page": page.number, "rect": [round(x, 2) for x in f.rect], "kind": f.kind,
                              "source": f.source, "status": f"resolution:{why}"})
            continue
        add(f, entry)
    # unlabelled occurrences: frozen map + values already resolved in this document (duplicates)
    for f in lookup_findings(lines, resolver.lookup):
        entry = resolver.entry(f.value)
        if entry is not None:
            res.findings[f"lookup:{entry.kind}"] += 1
            add(f, entry)
    return edits


# ------------------------------------------------------------------ fingerprints for preservation checks


def _chars(page) -> list[tuple[str, tuple[float, float, float, float]]]:
    out = []
    for ln in page_lines(page):
        for c, b in zip(ln.text, ln.boxes):
            if not c.isspace():
                out.append((c, b))
    return out


def _inside(b, rects) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return any(r.x0 - 0.5 <= cx <= r.x1 + 0.5 and r.y0 - 0.5 <= cy <= r.y1 + 0.5 for r in rects)


def _rkey(b) -> tuple:
    return tuple(round(x * 2) / 2 for x in b)


def _covered(rect, exclude) -> bool:
    r = pymupdf.Rect(rect)
    return any((x + (-0.5, -0.5, 0.5, 0.5)).contains(r) for x in exclude)


def _drawings_fp(page, exclude=()) -> str:
    """Vector fingerprint; drawings fully inside an OCR edit rect (glyph outlines, redaction fill) excluded."""
    h = hashlib.sha256()
    try:
        for d in page.get_drawings():
            if exclude and _covered(d.get("rect", (0, 0, 0, 0)), exclude):
                continue
            h.update(repr((d.get("type"), _rkey(d.get("rect", (0, 0, 0, 0))), len(d.get("items", ())),
                           d.get("color"), d.get("fill"), round(d.get("width") or 0, 2))).encode())
    except Exception:  # noqa: BLE001
        h.update(b"error")
    return h.hexdigest()


def _images_fp(page, exclude=()) -> list:
    """Image placements + content digests; an image intersecting an OCR edit rect keeps only its placement."""
    try:
        return sorted((_rkey(i["bbox"]), i.get("digest", b"").hex() if isinstance(i.get("digest"), bytes)
                       and not any(pymupdf.Rect(i["bbox"]).intersects(x) for x in exclude) else "")
                      for i in page.get_image_info(hashes=True))
    except Exception:  # noqa: BLE001
        return ["error"]


def _links_fp(page) -> list:
    return sorted(repr(sorted((k, str(v)) for k, v in ln.items() if k in ("kind", "page", "uri", "file", "from")))
                  for ln in page.get_links())


def _geometry(doc) -> list:
    return [(tuple(p.mediabox), tuple(p.cropbox), p.rotation) for p in doc]


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", _normalize(s))


# ------------------------------------------------------------------ string substitution (non-page text)


class _Subst:
    def __init__(self, pairs: dict[str, str]):
        self.pairs = sorted(pairs.items(), key=lambda kv: -len(kv[0]))
        alts = [r"\s+".join(map(re.escape, k.split())) for k, _ in self.pairs if k.split()]
        self.rx = re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.I) if alts else None
        self.low = {_normalize(k): v for k, v in self.pairs}

    def __call__(self, s: str) -> tuple[str, int]:
        if not s or self.rx is None:
            return s, 0
        n = 0

        def rep(m):
            nonlocal n
            n += 1
            return self.low.get(_normalize(m.group(0)), m.group(0))
        return self.rx.sub(rep, s), n


def _document_strings(doc) -> list[str]:
    out = [v for k, v in (doc.metadata or {}).items() if k in _INFO_KEYS and v]
    out += [str(t[1]) for t in doc.get_toc(simple=True)]
    for name in doc.embfile_names():
        info = doc.embfile_info(name)
        out += [str(info.get(k) or "") for k in ("filename", "ufilename", "desc")]
    for page in doc:
        for a in page.annots() or []:
            info = a.info or {}
            out += [info.get(k) or "" for k in ("content", "title", "subject")]
        for w in page.widgets() or []:
            if isinstance(w.field_value, str):
                out.append(w.field_value)
    return [s for s in out if s]


def _lookup_document_strings(doc, resolver: Resolver) -> None:
    """Frozen-map lookups inside metadata/annotation/form/bookmark/attachment strings (unlabelled mentions)."""
    from .pdf_titleblock import ngram_spans
    for s in _document_strings(doc):
        for a, b in ngram_spans(s):
            if len(_normalize(s[a:b])) >= 4 and resolver.lookup(s[a:b]):
                resolver.local.setdefault(_normalize(s[a:b]), resolver.entry(s[a:b]))


def _subst_document(doc, sub: _Subst, res: ScopedResult, resolver: Resolver) -> None:
    md = dict(doc.metadata or {})
    new_md = {}
    for k in _INFO_KEYS:
        v = md.get(k) or ""
        nv, n = sub(v)
        if k == "author" and v and not n:  # whole-field author not seen on the pages: resolve as a person
            entry, _ = resolver.resolve("person", v)
            if entry is not None:
                nv, n = entry.pseudonym, 1
        new_md[k] = nv
        res.metadata_replacements += n
    if res.metadata_replacements:
        doc.set_metadata({**{k: md.get(k, "") for k in ("creationDate", "modDate")}, **new_md})
    try:
        xmp = doc.get_xml_metadata()
    except Exception:  # noqa: BLE001
        xmp = ""
    if xmp:
        import html
        nx, n = sub(xmp)
        esc = _Subst({html.escape(k): html.escape(v) for k, v in sub.pairs})
        nx, n2 = esc(nx)
        if n + n2:
            doc.set_xml_metadata(nx)
            res.metadata_replacements += n + n2
    toc = doc.get_toc(simple=False)
    changed = 0
    for item in toc:
        nt, n = sub(item[1])
        if n:
            item[1] = nt
            changed += n
    if changed:
        doc.set_toc(toc)
        res.toc_replacements += changed
    for name in doc.embfile_names():
        info = doc.embfile_info(name)
        upd = {}
        for k in ("filename", "ufilename", "desc"):
            nv, n = sub(info.get(k) or "")
            if n:
                upd[k] = nv
                res.attachment_replacements += n
        if upd:
            doc.embfile_upd(name, filename=upd.get("filename"), ufilename=upd.get("ufilename"), desc=upd.get("desc"))
    for page in doc:
        for a in page.annots() or []:
            if a.type[0] == pymupdf.PDF_ANNOT_REDACT:
                continue
            info = a.info or {}
            upd = {}
            for k in ("content", "title", "subject"):
                nv, n = sub(info.get(k) or "")
                if n:
                    upd[k] = nv
                    res.annotation_replacements += n
            if upd:
                a.set_info(**upd)
                a.update()
        for w in page.widgets() or []:
            if isinstance(w.field_value, str):
                nv, n = sub(w.field_value)
                if n:
                    w.field_value = nv
                    w.update()
                    res.widget_replacements += n


# ------------------------------------------------------------------ main entry


def _ocr_rects(plans, pno) -> list:
    return [pymupdf.Rect(e.rect) for e in plans.get(pno, []) if e.ocr]


def transform_pdf_scoped(src: str | Path, dst: str | Path, resolver: Resolver, *, overwrite: bool = False,
                         render_check: bool = True, ocr: bool = False) -> ScopedResult:
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("output path must differ from source path")
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    res = ScopedResult()
    resolver.begin_document()
    doc = pymupdf.open(src)
    tmp = dst.with_name(dst.name + ".partial")
    try:
        if not doc.is_pdf:
            res.reason = "not_pdf"
            return res
        if doc.needs_pass:
            res.reason = "encrypted"
            return res
        res.pages = doc.page_count
        geometry = _geometry(doc)
        plans: dict[int, list[Edit]] = {}
        before: dict[int, tuple] = {}
        ocr_lines: dict[int, list] = {}
        for page in doc:
            lines = None
            if not page.get_text("text").strip():
                res.textless_pages += 1
                if ocr:
                    from .pdf_ocr import ocr_page_lines
                    lines = ocr_lines[page.number] = ocr_page_lines(page)
                    res.ocr_pages += 1
            edits = plan_page(page, resolver, res, lines, ocr=lines is not None)
            if any(a.type[0] == pymupdf.PDF_ANNOT_REDACT for a in (page.annots() or [])) and edits:
                res.reason = "preexisting_redaction_annotation"
                return res
            if edits:
                plans[page.number] = edits
        # Second planning sweep: values resolved on later pages also occur on earlier ones.
        for page in doc:
            ol = ocr_lines.get(page.number)
            extra = [e for e in plan_page(page, resolver, ScopedResult(), ol, ocr=ol is not None)
                     if not any(pymupdf.Rect(e.rect).intersects(pymupdf.Rect(p.rect)) for p in plans.get(page.number, []))]
            if extra:
                plans.setdefault(page.number, []).extend(extra)
        for page in doc:
            ex = _ocr_rects(plans, page.number)
            before[page.number] = (_drawings_fp(page, ex), _images_fp(page, ex), _links_fp(page))
        _lookup_document_strings(doc, resolver)
        pairs = {k: v.pseudonym for k, v in resolver.local.items()}
        res.resolved_values = list(pairs)
        all_edits = [e for es in plans.values() for e in es]
        res.edits_planned = len(all_edits)
        keep: dict[int, list] = {}
        for pno, edits in plans.items():
            page = doc[pno]
            rects = [pymupdf.Rect(e.rect) for e in edits]
            keep[pno] = [(c, _rkey(b)) for c, b in _chars(page) if not _inside(b, rects)]
            any_ocr = any(e.ocr for e in edits)
            for e in edits:
                r = pymupdf.Rect(e.rect)
                if e.ocr:  # raster/outline glyphs: blank pixels + fully covered outlines; no fill (never occlude lines)
                    page.add_redact_annot(r + (-0.5, -0.5, 0.5, 0.5), fill=False)
                    continue
                inset = min(r.width, r.height) * 0.15
                page.add_redact_annot(r + (0.3, inset, -0.3, -inset) if r.width > 1 else r)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS if any_ocr else pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED if any_ocr else pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            for e in edits:
                ang = math.degrees(math.atan2(-e.dir[1], e.dir[0]))
                mat = pymupdf.Matrix(ang) if abs(ang) > 0.01 else None
                kw = {"morph": (pymupdf.Point(e.origin), mat)} if mat is not None else {}
                page.insert_text(pymupdf.Point(e.origin), e.new, fontsize=e.fontsize, fontname=FONT,
                                 color=_rgb(e.color), **kw)
                res.fitted_used += e.fitted
        sub = _Subst(pairs)
        _subst_document(doc, sub, res, resolver)
        if not all_edits and not res.changes:
            res.reason = "no_resolved_identities"
            return res
        dst.parent.mkdir(parents=True, exist_ok=True)
        doc.save(tmp, garbage=3, deflate=True, no_new_id=True)  # reproducible output bytes
    finally:
        doc.close()

    reason = _verify(src, tmp, plans, keep, before, geometry, pairs, res, render_check)
    if reason:
        res.reason = reason
        tmp.unlink(missing_ok=True)
        return res
    tmp.replace(dst)
    res.ok = True
    return res


def _verify(src, out_path, plans, keep, before, geometry, pairs, res: ScopedResult, render_check: bool) -> str:
    out = pymupdf.open(out_path)
    try:
        if out.page_count != res.pages or _geometry(out) != geometry:
            return "page_geometry_changed"
        failed = 0
        for page in out:
            pno = page.number
            ex = _ocr_rects(plans, pno)
            fp = (_drawings_fp(page, ex), _images_fp(page, ex), _links_fp(page))
            if fp[0] != before[pno][0]:
                return "vector_graphics_changed"
            if fp[1] != before[pno][1]:
                return "images_changed"
            if fp[2] != before[pno][2]:
                return "links_changed"
            edits = plans.get(pno, [])
            if not edits:
                continue
            chars = _chars(page)
            have = Counter((c, _rkey(b)) for c, b in chars)
            need = Counter(keep[pno])
            if need - have:
                return "text_outside_edit_regions_changed"
            lines = page_lines(page)
            for e in edits:
                r = pymupdf.Rect(e.rect)
                region = r + (-1, -1, 1, 1)
                region |= _inserted_rect(e)
                inside = "".join(c for ln in lines for c, b in zip(ln.text, ln.boxes)
                                 if _inside(b, [region + (-0.5, -0.5, 0.5, 0.5)]))
                ok = _compact(e.old) not in _compact(inside) and _compact(e.new) in _compact(inside)
                if ok and render_check:
                    ok = _has_ink(page, _inserted_rect(e))
                if ok and e.ocr:  # the original must no longer be legible (independent re-OCR of the region)
                    from .pdf_ocr import region_text
                    try:
                        ok = _compact(e.old) not in _compact(region_text(page, r))
                    except Exception:  # noqa: BLE001 - cannot verify -> fail closed
                        ok = False
                    res.ocr_edits_verified += ok
                e.status = "verified" if ok else "failed:rewrite_not_verified"
                failed += not ok
                res.trace.append({"page": pno, "rect": [round(x, 2) for x in e.rect], "kind": e.kind,
                                  "source": e.source, "fitted": e.fitted, "status": e.status})
        res.edits_verified = sum(e.status == "verified" for es in plans.values() for e in es)
        # whole-document residual of every resolved value (independent inspector, all locations)
        from .pdf_residual import inspect_pdf
        rep = inspect_pdf(out_path, list(pairs))
        if not rep.ok:
            return "independent_inspection_incomplete"
        for k, v in rep.residual.items():
            if v:
                res.residual[k] += v
        if failed:
            return "edit_verification_failed"
        if rep.total:
            return "resolved_value_residual"
        return ""
    finally:
        out.close()


def _inserted_rect(e: Edit) -> pymupdf.Rect:
    w = pymupdf.get_text_length(e.new, fontname=FONT, fontsize=e.fontsize)
    c, s = e.dir
    ox, oy = e.origin
    asc, desc = e.fontsize * 1.075, e.fontsize * 0.3
    # baseline from origin along dir; ascent perpendicular (towards -normal in page space)
    nx, ny = s, -c  # "up" for text direction (c, s) in y-down space
    pts = [(ox + c * u + nx * v, oy + s * u + ny * v) for u in (0, w) for v in (-desc, asc)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))


def _has_ink(page, rect: pymupdf.Rect) -> bool:
    clip = (rect * page.rotation_matrix) & page.rect  # text space is unrotated; pixmap clip is visual space
    if clip.is_empty or clip.width < 0.5 or clip.height < 0.5:
        return False
    zoom = max(1.0, min(8.0, 24.0 / max(min(clip.width, clip.height), 1e-3)))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    data = pix.samples
    if not data:
        return False
    lo, hi = min(data), max(data)
    return hi - lo >= 64
