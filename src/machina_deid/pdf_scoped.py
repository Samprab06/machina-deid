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
from .pdf_ocr import ocr_page_lines
from .pdf_links import link_snapshot, restore_links, rewrite_link_actions, verify_links
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
    checks: Counter = field(default_factory=Counter)  # every failing verification check (value-free codes)
    link_replacements: int = 0
    links_restored: int = 0
    textless_pages: int = 0
    ocr: Counter = field(default_factory=Counter)  # OCR guard/outcome codes
    ocr_pages: int = 0
    ocr_edits_verified: int = 0
    trace: list[dict] = field(default_factory=list)  # value-free per-edit records (page, rect, kind, status)
    resolved_values: list[str] = field(default_factory=list, repr=False)  # raw, memory only

    @property
    def changes(self) -> int:
        return (self.edits_verified + self.metadata_replacements + self.annotation_replacements
                + self.widget_replacements + self.toc_replacements + self.attachment_replacements
                + self.link_replacements)

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


_CONTEXTUAL_KINDS = frozenset({"person", "org", "project", "account", "address"})
_PERSON_CUE = re.compile(r"\b(?:attention|questions to|contact|reviewed by|prepared by|approved by)\s*[:=]?\s*$")


def _lookup_in_context(value: str, container: str, resolver: Resolver):
    """Use context-free lookup only for structured identifiers."""
    match = resolver.lookup(value)
    if match and match[0] in _CONTEXTUAL_KINDS:
        candidate = _normalize(value)
        text = _normalize(container).strip(" :;,|")
        if len(candidate.split()) < 2:
            return None
        if text != candidate:
            if match[0] != "person" or not text.endswith(candidate):
                return None
            if not _PERSON_CUE.search(text[:-len(candidate)]):
                return None
    return match


def plan_page(page, resolver: Resolver, res: ScopedResult, ocr_lines=None) -> list[Edit]:
    """Resolve and plan non-overlapping edits from native text or OCR lines."""
    is_ocr = ocr_lines is not None
    lines = ocr_lines if is_ocr else page_lines(page)
    found: list[Finding] = scan_page_lines(lines)
    for f in found:
        res.findings[f"{'ocr:' if is_ocr else ''}{f.source}:{f.kind}"] += 1
    edits: list[Edit] = []

    def add(f: Finding, entry) -> None:
        ln = f.line
        r = f.rect
        if is_ocr:
            r = r + (-1, -1, 1, 1)
        for e in edits:
            if pymupdf.Rect(e.rect).intersects(r) and abs(pymupdf.Rect(e.rect) & r) > 0.25 * abs(r):
                return
        new, fs, is_fit = _choose_text(entry.pseudonym, entry.fitted, _extent(ln, f.start, f.end), ln.size)
        org = ln.origins[f.start] if ln.origins else ln.origin
        edits.append(Edit(page.number, f.kind, ("ocr:" if is_ocr else "") + f.source, f.value, new, tuple(r),
                          ln.dir, tuple(org), ln.size, ln.color, is_fit, fs))

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
        if not _lookup_in_context(f.value, f.line.text, resolver):
            continue
        entry = resolver.entry(f.value)
        if entry is not None:
            res.findings[f"{'ocr:' if is_ocr else ''}lookup:{entry.kind}"] += 1
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


def _covered(r, rects, pad: float = 0.5) -> bool:
    r = pymupdf.Rect(r)
    return any(r in (q + (-pad, -pad, pad, pad)) for q in rects)


def _drawings_fp(page, ocr_rects=()) -> str:
    """Vector drawings fingerprint. Paths *entirely inside* an OCR edit rect (outlined glyphs removed by the
    covered-line-art redaction) are excluded; everything else must be unchanged."""
    h = hashlib.sha256()
    try:
        for d in page.get_drawings():
            rect = d.get("rect", (0, 0, 0, 0))
            if ocr_rects and _covered(rect, ocr_rects):
                continue
            h.update(repr((d.get("type"), _rkey(rect), len(d.get("items", ())),
                           d.get("color"), d.get("fill"), round(d.get("width") or 0, 2))).encode())
    except Exception:  # noqa: BLE001
        h.update(b"error")
    return h.hexdigest()


def _images_fp(page, ocr_rects=()) -> list:
    """Image placements and pixel digests; an image touched by an OCR edit keeps its placement but its digest
    is exempt (its pixels under the edit were cleared); pixels outside edits are checked by rendering."""
    try:
        out = []
        for i in page.get_image_info(hashes=True):
            dg = i.get("digest", b"").hex() if isinstance(i.get("digest"), bytes) else ""
            if ocr_rects and any(pymupdf.Rect(i["bbox"]).intersects(q) for q in ocr_rects):
                dg = "ocr-touched"
            out.append((_rkey(i["bbox"]), dg))
        return sorted(out)
    except Exception:  # noqa: BLE001
        return ["error"]


def _render_gray(page, zoom: float):
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)


def _render_outside_unchanged(src_page, out_page, rects, zoom: float = 1.0) -> bool:
    """Rendered pixels outside the (padded) edit rects are unchanged: the OCR rewrite touched nothing else."""
    a, b = _render_gray(src_page, zoom), _render_gray(out_page, zoom)
    if (a.width, a.height) != (b.width, b.height):
        return False
    mat = src_page.rotation_matrix * pymupdf.Matrix(zoom, zoom)
    masks = []
    for r in rects:
        q = (pymupdf.Rect(r) * mat) + (-3, -3, 3, 3)
        masks.append((max(0, int(q.x0)), max(0, int(q.y0)), min(a.width, int(q.x1) + 1), min(a.height, int(q.y1) + 1)))
    sa, sb, w = a.samples, b.samples, a.width
    bad = 0
    for y in range(a.height):
        row = y * w
        if sa[row:row + w] == sb[row:row + w]:
            continue
        for x in range(w):
            if abs(sa[row + x] - sb[row + x]) > 24 and not any(x0 <= x < x1 and y0 <= y < y1
                                                                 for x0, y0, x1, y1 in masks):
                bad += 1
    return bad <= max(2, a.width * a.height // 20000)


def _geometry(doc) -> list:
    return [(tuple(p.mediabox), tuple(p.cropbox), p.rotation) for p in doc]


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", _normalize(s))


# ------------------------------------------------------------------ string substitution (non-page text)


_B0, _B1 = r"(?<![^\W_])", r"(?![^\W_])"  # boundary = not a letter/digit (underscore separates, as in paths)


class _Subst:
    """Exact, case-insensitive, boundary-delimited substitution of resolved values (whitespace-tolerant)."""

    def __init__(self, pairs: dict[str, str]):
        self.pairs = sorted(pairs.items(), key=lambda kv: -len(kv[0]))
        alts = [r"\s+".join(map(re.escape, k.split())) for k, _ in self.pairs if k.split()]
        self.rx = re.compile(_B0 + r"(?:" + "|".join(alts) + r")" + _B1, re.I) if alts else None
        self.low = {_normalize(k): v for k, v in self.pairs}

    def __call__(self, s: str) -> tuple[str, int]:
        if not s or self.rx is None:
            return s, 0
        n = 0

        def rep(m):
            nonlocal n
            v = self.low.get(_normalize(m.group(0)))
            if v is None:  # regex case-folding matched a form the normalizer does not equate: leave it
                return m.group(0)
            n += 1
            return v
        return self.rx.sub(rep, s), n


_SKIP_INFO = frozenset(("CreationDate", "ModDate", "Trapped"))


def _info_items(doc) -> list[tuple[int, str, str]]:
    """(xref, key, text) of every string entry of the trailer /Info dictionary (standard and custom keys)."""
    typ, val = doc.xref_get_key(-1, "Info")
    if typ != "xref":
        return []
    x = int(val.split()[0])
    out = []
    for k in doc.xref_get_keys(x):
        if k in _SKIP_INFO:
            continue
        t, v = doc.xref_get_key(x, k)
        if t == "string" and v:
            out.append((x, k, v))
    return out


def _metadata_streams(doc) -> list[int]:
    out = []
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "Type")[1] == "/Metadata" and doc.xref_is_stream(xref):
                out.append(xref)
        except Exception:  # noqa: BLE001
            continue
    return out


def _decode_xmp(data: bytes) -> tuple[str, str] | None:
    """(text, encoding) or None when the packet is not cleanly decodable (left untouched -> residual check)."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                return None
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return None


def _xmp_texts(doc) -> list[str]:
    import html
    out = []
    for x in _metadata_streams(doc):
        dec = _decode_xmp(doc.xref_stream(x) or b"")
        if dec:
            out += [html.unescape(t) for t in re.split(r"<[^>]*>", dec[0]) if t.strip()]
    return out


def _document_strings(doc) -> list[str]:
    out = [v for _, _, v in _info_items(doc)]
    out += _xmp_texts(doc)
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
            if len(_normalize(s[a:b])) >= 4 and _lookup_in_context(s[a:b], s, resolver):
                resolver.local.setdefault(_normalize(s[a:b]), resolver.entry(s[a:b]))


def _resolve_metadata_author(doc, resolver: Resolver) -> None:
    """A whole-field Info author not otherwise seen in the document is resolved as a person - only when it is
    a validated person value (same validator as page findings). Done *before* the substitution table is built
    so the Info dictionary, every XMP packet (dc:creator, pdf:Author) and the residual check all see it."""
    from .pdf_titleblock import valid_value
    v = ((doc.metadata or {}).get("author") or "").strip()
    if not v or _normalize(v) in resolver.local:
        return
    if _Subst({k: e.pseudonym for k, e in resolver.local.items()})(v)[1]:
        return
    if valid_value("person", v):
        resolver.resolve("person", v)


def _subst_document(doc, sub: _Subst, res: ScopedResult, resolver: Resolver) -> None:
    import html
    for x, k, v in _info_items(doc):
        nv, n = sub(v)
        if n:
            doc.xref_set_key(x, k, pymupdf.get_pdf_str(nv))
            res.metadata_replacements += n
    if doc.xref_get_key(-1, "Info")[0] == "dict":  # rare: direct /Info dictionary in the trailer
        md = dict(doc.metadata or {})
        new_md, n_all = {}, 0
        for k in _INFO_KEYS:
            nv, n = sub(md.get(k) or "")
            new_md[k], n_all = nv, n_all + n
        if n_all:
            doc.set_metadata({**{k: md.get(k, "") for k in ("creationDate", "modDate")}, **new_md})
            res.metadata_replacements += n_all
    esc = _Subst({html.escape(k): html.escape(v) for k, v in sub.pairs})
    for x in _metadata_streams(doc):
        raw = doc.xref_stream(x) or b""
        dec = _decode_xmp(raw)
        if dec is None:
            res.checks["xmp_undecodable"] += 1
            continue
        text, enc = dec
        nx, n = sub(text)
        nx, n2 = esc(nx)
        if n + n2:
            doc.update_stream(x, nx.encode(enc))
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
    res.link_replacements += rewrite_link_actions(doc, sub)


# ------------------------------------------------------------------ main entry


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
        ocr_cache: dict[int, list] = {}
        for page in doc:
            if not page.get_text("text").strip():
                res.textless_pages += 1
                if ocr:
                    try:
                        ocr_cache[page.number] = ocr_page_lines(page)
                        res.ocr_pages += 1
                    except (RuntimeError, ValueError):
                        res.ocr["scan_unavailable"] += 1
            edits = plan_page(page, resolver, res, ocr_cache.get(page.number))
            if any(a.type[0] == pymupdf.PDF_ANNOT_REDACT for a in (page.annots() or [])) and edits:
                res.reason = "preexisting_redaction_annotation"
                return res
            if edits:
                plans[page.number] = edits
        # Second planning sweep: values resolved on later pages also occur on earlier ones.
        for page in doc:
            extra = [e for e in plan_page(page, resolver, ScopedResult(), ocr_cache.get(page.number))
                     if not any(pymupdf.Rect(e.rect).intersects(pymupdf.Rect(p.rect)) for p in plans.get(page.number, []))]
            if extra:
                plans.setdefault(page.number, []).extend(extra)
        before = {}
        for page in doc:
            orects = [pymupdf.Rect(e.rect) for e in plans.get(page.number, []) if e.source.startswith("ocr")]
            before[page.number] = (_drawings_fp(page, orects), _images_fp(page, orects))
        _lookup_document_strings(doc, resolver)
        _resolve_metadata_author(doc, resolver)
        pairs = {k: v.pseudonym for k, v in resolver.local.items()}
        res.resolved_values = list(pairs)
        all_edits = [e for es in plans.values() for e in es]
        res.edits_planned = len(all_edits)
        res.ocr["edits_planned"] += sum(e.source.startswith("ocr") for e in all_edits)
        keep: dict[int, list] = {}
        for pno, edits in plans.items():
            page = doc[pno]
            rects = [pymupdf.Rect(e.rect) for e in edits]
            keep[pno] = [(c, _rkey(b)) for c, b in _chars(page) if not _inside(b, rects)]
            snap = link_snapshot(doc, page)
            order = [x for x, _, _ in page.annot_xrefs()]
            is_ocr = any(e.source.startswith("ocr") for e in edits)
            for e in edits:
                r = pymupdf.Rect(e.rect)
                if e.source.startswith("ocr"):  # scanned/outlined glyphs: clear the word box itself
                    page.add_redact_annot(r, fill=False)
                else:
                    inset = min(r.width, r.height) * 0.15
                    page.add_redact_annot(r + (0.3, inset, -0.3, -inset) if r.width > 1 else r)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS if is_ocr else pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED if is_ocr else pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            res.links_restored += restore_links(doc, page, snap, order)
            for e in edits:
                if not e.new:
                    continue
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
        doc.save(tmp, garbage=3, deflate=True, no_new_id=True)
    finally:
        doc.close()

    reason = _verify(src, tmp, plans, keep, before, geometry, pairs, res, render_check, ocr)
    if reason:
        res.reason = reason
        tmp.unlink(missing_ok=True)
        return res
    tmp.replace(dst)
    res.ok = True
    return res


# Document-level verdict priority: the first failing check in this order is ``reason``; *all* failing checks
# are counted in ``res.checks`` (value-free) so a doc failing early still gets every edit verified/classified.
_CHECK_ORDER = ("page_geometry_changed", "vector_graphics_changed", "images_changed", "ocr_region_overflow",
                "links_changed", "text_outside_edit_regions_changed", "independent_inspection_incomplete",
                "edit_verification_failed", "resolved_value_residual")


def _verify(src, out_path, plans, keep, before, geometry, pairs, res: ScopedResult, render_check: bool,
            ocr=None) -> str:
    out = pymupdf.open(out_path)
    srcdoc = pymupdf.open(src)
    failed_checks: Counter = Counter()
    try:
        if out.page_count != res.pages or _geometry(out) != geometry:
            res.checks["page_geometry_changed"] += 1
            return "page_geometry_changed"
        for k, v in verify_links(srcdoc, out, pairs).items():
            res.checks[k] += v
            failed_checks["links_changed"] += v
        for page in out:
            pno = page.number
            edits = plans.get(pno, [])
            ocr_rects = [pymupdf.Rect(e.rect) for e in edits if e.source.startswith("ocr")]
            if _drawings_fp(page, ocr_rects) != before[pno][0]:
                failed_checks["vector_graphics_changed"] += 1
            if _images_fp(page, ocr_rects) != before[pno][1]:
                failed_checks["images_changed"] += 1
            if ocr_rects and not _render_outside_unchanged(srcdoc[pno], page, ocr_rects + [
                    _inserted_rect(e) for e in edits if e.source.startswith("ocr")]):
                failed_checks["ocr_region_overflow"] += 1
            if not edits:
                continue
            chars = _chars(page)
            have = Counter((c, _rkey(b)) for c, b in chars)
            need = Counter(keep[pno])
            if need - have:
                failed_checks["text_outside_edit_regions_changed"] += 1
            lines = page_lines(page)
            for e in edits:
                r = pymupdf.Rect(e.rect)
                region = r + (-1, -1, 1, 1)
                region |= _inserted_rect(e)
                inside = "".join(c for ln in lines for c, b in zip(ln.text, ln.boxes)
                                 if _inside(b, [region + (-0.5, -0.5, 0.5, 0.5)]))
                if e.source.startswith("ocr"):
                    why = _verify_ocr_edit(srcdoc[pno], page, e, inside)
                    res.ocr_edits_verified += not why
                elif _compact(e.old) in _compact(inside):
                    why = "old_present"
                elif e.new and _compact(e.new) not in _compact(inside):
                    why = "new_absent"
                elif render_check and e.new and not _has_ink(page, _inserted_rect(e)):
                    why = "no_ink"
                else:
                    why = ""
                e.status = "verified" if not why else f"failed:{why}"
                if why:
                    failed_checks["edit_verification_failed"] += 1
                    res.checks[f"edit:{why}"] += 1
                res.trace.append({"page": pno, "rect": [round(x, 2) for x in e.rect], "kind": e.kind,
                                  "source": e.source, "fitted": e.fitted, "status": e.status})
        res.edits_verified = sum(e.status == "verified" for es in plans.values() for e in es)
        # whole-document residual of every resolved value (independent inspector, all locations)
        from .pdf_residual import inspect_pdf
        rep = inspect_pdf(out_path, list(pairs), word_boundaries=True)
        if not rep.ok:
            failed_checks["independent_inspection_incomplete"] += 1
        for k, v in rep.residual.items():
            if v:
                res.residual[k] += v
        if rep.total:
            failed_checks["resolved_value_residual"] += 1
        res.checks.update(failed_checks)
        return next((k for k in _CHECK_ORDER if failed_checks.get(k)), "")
    finally:
        out.close()
        srcdoc.close()


def _verify_ocr_edit(src_page, out_page, e: Edit, inside: str) -> str:
    """OCR edit: pseudonym extractable + inked, the word box pixels actually changed, and (independently)
    a fresh OCR of the box no longer reads the original value."""
    if _compact(e.new) not in _compact(inside):
        return "new_absent"
    if not _has_ink(out_page, _inserted_rect(e)):
        return "no_ink"
    r = pymupdf.Rect(e.rect)
    clip = (r * src_page.rotation_matrix) & src_page.rect
    if clip.is_empty:
        return "region_empty"
    a = src_page.get_pixmap(clip=clip, colorspace=pymupdf.csGRAY, alpha=False).samples
    b = out_page.get_pixmap(clip=clip, colorspace=pymupdf.csGRAY, alpha=False).samples
    if a == b:
        return "region_not_cleared"
    from .pdf_ocr import region_text
    try:
        txt = region_text(out_page, r)
    except (RuntimeError, ValueError):
        return "ocr_reverify_unavailable"
    if _compact(e.old) in _compact(txt):
        return "old_ocr_readable"
    return ""


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
