"""PDF de-identification with PyMuPDF.

Steps: open source read-only -> clear document metadata (Info + XMP) -> inspect
and preserve attachments -> for each explicitly mapped string, add real redaction
annotations over matched visible text (with replacement text) and apply them ->
save to a *new* path -> reopen and verify: page count/dimensions unchanged,
metadata cleared, old strings absent from text/metadata/outline/annotations.

Matching uses PyMuPDF text search (case-insensitive, single-line). Text a search
cannot match (split across lines, in images, outlined glyphs) stays; the
verification step reports it as ``residual`` and ``ok`` is False.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

from .idmap import IdentityMap

_INFO_KEYS = ("title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate")


@dataclass
class PdfResult:
    ok: bool = False
    pages: int = 0
    pages_preserved: bool = False
    dimensions_preserved: bool = False
    metadata_cleared: bool = False
    xmp_cleared: bool = False
    attachments_before: int = 0
    attachments_after: int = 0
    file_annots_before: int = 0
    file_annots_after: int = 0
    has_extractable_text: bool = False
    matches: dict[int, int] = field(default_factory=dict)  # identity index -> redactions applied
    residual: dict[int, int] = field(default_factory=dict)  # identity index -> remaining occurrences
    warnings: list[str] = field(default_factory=list)
    output_sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def _dims(doc) -> list[tuple[float, float, float, float, int]]:
    return [(round(p.rect.width, 3), round(p.rect.height, 3), round(p.mediabox.width, 3),
             round(p.mediabox.height, 3), p.rotation) for p in doc]


def _attachment_state(doc) -> tuple[int, int]:
    emb = len(doc.embfile_names()) if doc.is_pdf else 0
    fa = sum(1 for p in doc for a in (p.annots() or []) if a.type[0] == pymupdf.PDF_ANNOT_FILE_ATTACHMENT)
    return emb, fa


def _all_text(doc) -> str:
    parts = [p.get_text("text") for p in doc]
    parts += [str(v) for v in (doc.metadata or {}).values() if v]
    parts += [t[1] for t in doc.get_toc()]
    parts += [(a.info.get("content", "") or "") + (a.info.get("title", "") or "") for p in doc for a in (p.annots() or [])]
    xmp = doc.get_xml_metadata()
    if xmp:
        parts.append(xmp)
    return "\n".join(parts)


def _count(text: str, needle: str) -> int:
    return text.casefold().count(needle.casefold())


def transform_pdf(src: str | Path, dst: str | Path, idmap: IdentityMap, *, clear_metadata: bool = True,
                  overwrite: bool = False) -> PdfResult:
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("output path must differ from source path")
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    res = PdfResult()
    pairs = idmap.ordered_strings()
    index = {old: i for i, (old, _) in enumerate(idmap.strings.items())}

    doc = pymupdf.open(src)
    try:
        if not doc.is_pdf:
            raise ValueError("not a PDF")
        if doc.needs_pass:
            raise ValueError("encrypted PDF")
        res.pages = doc.page_count
        dims_before = _dims(doc)
        res.attachments_before, res.file_annots_before = _attachment_state(doc)
        res.has_extractable_text = any(p.get_text("text").strip() for p in doc)
        if not res.has_extractable_text:
            res.warnings.append("no extractable text: visible redaction not exercised")

        if clear_metadata:
            doc.set_metadata({k: "" for k in _INFO_KEYS} | {"format": doc.metadata.get("format", "")})
            doc.del_xml_metadata()

        for page in doc:
            for old, new in pairs:
                rects = page.search_for(old)
                for r in rects:
                    fs = max(4.0, min(12.0, r.height * 0.8))
                    page.add_redact_annot(r, text=new, fontname="helv", fontsize=fs, fill=(1, 1, 1),
                                          text_color=(0, 0, 0))
                if rects:
                    res.matches[index[old]] = res.matches.get(index[old], 0) + len(rects)
            if page.first_annot is not None:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        if any(res.matches.values()) and any(pymupdf.PDF_ANNOT_REDACT == a.type[0]
                                              for p in doc for a in (p.annots() or [])):
            res.warnings.append("unapplied redaction annotations remain")

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".partial")
        doc.save(tmp, garbage=4, deflate=True, clean=True)
    finally:
        doc.close()

    out = pymupdf.open(tmp)
    try:
        res.pages_preserved = out.page_count == res.pages
        res.dimensions_preserved = res.pages_preserved and _dims(out) == dims_before
        res.attachments_after, res.file_annots_after = _attachment_state(out)
        md = out.metadata or {}
        res.metadata_cleared = (not clear_metadata) or not any(md.get(k) for k in _INFO_KEYS)
        res.xmp_cleared = (not clear_metadata) or not out.get_xml_metadata()
        text = _all_text(out)
        for old, _ in pairs:
            n = _count(text, old)
            if n:
                res.residual[index[old]] = n
        # replacement text should be present wherever matches were applied
        for old, new in pairs:
            if res.matches.get(index[old]) and new.casefold() not in text.casefold():
                res.warnings.append(f"replacement for entry {index[old]} not found in extracted text")
    finally:
        out.close()

    res.ok = (res.pages_preserved and res.dimensions_preserved and res.metadata_cleared and res.xmp_cleared
              and not res.residual and res.attachments_after == res.attachments_before
              and res.file_annots_after == res.file_annots_before)
    if res.ok:
        tmp.replace(dst)
        res.output_sha256 = _sha256(dst)
    else:
        tmp.unlink()  # our own partial output (never the source); failed output is not kept
    return res
