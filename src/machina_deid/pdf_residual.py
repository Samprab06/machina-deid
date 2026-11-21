"""Independent, read-only PDF residual inspector.

Re-opens a (transformed) PDF and searches it for exact approved raw values in every place an identity
can survive a text-level redaction, using its own extraction code (it does not reuse
:mod:`pdf_transform` internals, so a blind spot there is not shared here):

* ``visible_text``   - page text from an independent extraction (``rawdict`` characters, so rotated and
  span-split text are reassembled; also a whitespace-insensitive pass for line/span splits)
* ``info_metadata``  - every key of the trailer ``/Info`` dictionary (not only the standard keys)
* ``xmp_metadata``   - the catalog XMP stream and any other ``/Type /Metadata`` stream (XML-unescaped)
* ``annotations``    - non-widget annotation content/title/subject/name and popup text
* ``form_fields``    - AcroForm widget names, values, labels, choice values, plus the AcroForm tree
* ``embedded_files`` - embedded-file names/descriptions/contents and file-attachment annotations
* ``outlines``       - bookmark titles and their link targets
* ``links``          - link URIs / file targets / named destinations (URL-unquoted)

The report contains only category counts, value *indices* and structural facts - never raw values.
Nothing is written; pages are never rasterized.
"""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote, unquote_plus

import pymupdf

CATEGORIES = ("visible_text", "info_metadata", "xmp_metadata", "annotations", "form_fields",
              "embedded_files", "outlines", "links")
_WS = re.compile(r"\s+")
_MAX_EMBED_BYTES = 64 << 20


@dataclass
class PdfResidualReport:
    ok: bool = False  # inspection completed (not "clean"; see ``clean``)
    reason: str = ""
    pages: int = 0
    dimensions: list[tuple[float, float, float, float, int]] = field(default_factory=list)
    values_checked: int = 0
    residual: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))
    residual_by_index: dict[int, int] = field(default_factory=dict)  # value index -> occurrences
    inspected: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))

    @property
    def total(self) -> int:
        return sum(self.residual.values())

    @property
    def clean(self) -> bool:
        return self.ok and self.total == 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total"], d["clean"] = self.total, self.clean
        return d


def page_geometry(doc) -> list[tuple[float, float, float, float, int]]:
    return [(round(p.rect.width, 3), round(p.rect.height, 3), round(p.mediabox.width, 3),
             round(p.mediabox.height, 3), p.rotation) for p in doc]


def _decode_bytes(b: bytes) -> str:
    parts = [b.decode("utf-8", "ignore"), b.decode("latin-1")]
    if b.startswith((b"\xff\xfe", b"\xfe\xff")) or b.count(b"\x00") > len(b) // 4:
        parts += [b.decode("utf-16-le", "ignore"), b.decode("utf-16-be", "ignore")]
    return "\n".join(parts)


class _Matcher:
    def __init__(self, values: list[str], *, word_boundaries: bool = False):
        self.values = [(i, v.casefold(), _WS.sub("", v.casefold())) for i, v in enumerate(values) if v.strip()]
        self.patterns = [
            (i, re.compile(r"(?<!\w)(?<!\w-)" + r"\s+".join(map(re.escape, v.split())) + r"(?!\w|-\w)"))
            for i, v, _ in self.values
        ] if word_boundaries else []

    def count(self, text: str, *, compact: bool = False) -> dict[int, int]:
        if not text:
            return {}
        low = text.casefold()
        if self.patterns:
            return {i: n for i, pattern in self.patterns if (n := sum(1 for _ in pattern.finditer(low)))}
        flat = _WS.sub("", low) if compact else ""
        out = {}
        for i, v, vc in self.values:
            n = low.count(v)
            if compact and vc:
                n = max(n, flat.count(vc))
            if n:
                out[i] = n
        return out


_REF = re.compile(r"(\d+) 0 R")
_NO_FOLLOW = {"P", "Parent", "Pages", "Page", "Dest", "Font", "Resources", "AP", "MK", "DR"}


def _xref_strings(doc, xref: int, depth: int = 0, seen: set | None = None, limit: int = 4096) -> list[str]:
    """String-ish values of a PDF dictionary, following indirect refs (bounded; never into pages)."""
    seen = seen if seen is not None else set()
    if xref in seen or depth > 8 or len(seen) > limit:
        return []
    seen.add(xref)
    out = []
    for k in doc.xref_get_keys(xref):
        typ, val = doc.xref_get_key(xref, k)
        out.append(val)
        if k in _NO_FOLLOW:
            continue
        for r in _REF.findall(val) if typ in ("xref", "array", "dict") else ():
            try:
                out += _xref_strings(doc, int(r), depth + 1, seen, limit)
            except (ValueError, RuntimeError):
                pass
    return out


def _visible_text(page) -> str:
    """Reassemble characters per line from rawdict (independent of the simple text extractor)."""
    lines = []
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES)
    for b in raw.get("blocks", []):
        for ln in b.get("lines", []):
            lines.append("".join(ch.get("c", "") for sp in ln.get("spans", []) for ch in sp.get("chars", [])))
    lines.append(page.get_text("text"))  # ordering-independent second extraction
    return "\n".join(lines)


def inspect_pdf(path: str | Path, values: list[str], *, word_boundaries: bool = False) -> PdfResidualReport:
    """Read-only rescan of ``path`` for each of ``values`` (exact, case-insensitive)."""
    rep = PdfResidualReport(values_checked=len(values))
    m = _Matcher(values, word_boundaries=word_boundaries)

    def add(cat: str, hits: dict[int, int]):
        for i, n in hits.items():
            rep.residual[cat] += n
            rep.residual_by_index[i] = rep.residual_by_index.get(i, 0) + n

    try:
        doc = pymupdf.open(Path(path))
    except Exception as exc:  # noqa: BLE001
        rep.reason = f"open_error:{type(exc).__name__}"
        return rep
    try:
        if not doc.is_pdf or doc.needs_pass:
            rep.reason = "not_pdf" if not doc.is_pdf else "encrypted"
            return rep
        rep.pages, rep.dimensions = doc.page_count, page_geometry(doc)

        for page in doc:
            rep.inspected["visible_text"] += 1
            add("visible_text", m.count(_visible_text(page), compact=True))

            for a in page.annots() or []:
                rep.inspected["annotations"] += 1
                info = a.info or {}
                txt = [info.get(k, "") or "" for k in ("content", "title", "subject", "name")]
                if a.type[0] == pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
                    rep.inspected["embedded_files"] += 1
                    fi = a.file_info or {}
                    add("embedded_files", m.count("\n".join(str(fi.get(k, "")) for k in
                                                            ("filename", "ufilename", "description", "desc"))))
                    try:
                        data = a.get_file()
                        if len(data) <= _MAX_EMBED_BYTES:
                            add("embedded_files", m.count(_decode_bytes(data)))
                    except Exception:  # noqa: BLE001
                        pass
                add("annotations", m.count("\n".join(txt)))

            for w in page.widgets() or []:
                rep.inspected["form_fields"] += 1
                parts = [w.field_name, w.field_value, w.field_label, w.button_caption]
                parts += [str(c) for c in (w.choice_values or [])]
                add("form_fields", m.count("\n".join(str(p) for p in parts if p not in (None, False))))

            for ln in page.get_links():
                rep.inspected["links"] += 1
                s = "\n".join(str(ln.get(k, "") or "") for k in ("uri", "file", "nameddest", "name"))
                add("links", m.count("\n".join((s, unquote(s), unquote_plus(s)))))

        # Info dictionary: every key, not only the standard ones
        typ, val = doc.xref_get_key(-1, "Info")
        if typ == "xref":
            rep.inspected["info_metadata"] += 1
            add("info_metadata", m.count("\n".join(_xref_strings(doc, int(val.split()[0])))))
        elif typ == "dict":
            rep.inspected["info_metadata"] += 1
            add("info_metadata", m.count(val))

        # XMP: catalog metadata and any other /Metadata streams
        for xref in range(1, doc.xref_length()):
            try:
                if doc.xref_get_key(xref, "Type")[1] != "/Metadata" or not doc.xref_is_stream(xref):
                    continue
                xml = _decode_bytes(doc.xref_stream(xref) or b"")
            except Exception:  # noqa: BLE001
                continue
            rep.inspected["xmp_metadata"] += 1
            add("xmp_metadata", m.count(xml + "\n" + html.unescape(xml)))

        # AcroForm field tree: only when no page widget exposed the fields (avoids double counting)
        typ, val = doc.xref_get_key(doc.pdf_catalog(), "AcroForm")
        if typ in ("xref", "dict") and not any(True for p in doc for _ in (p.widgets() or [])):
            rep.inspected["form_fields"] += 1
            txt = "\n".join(_xref_strings(doc, int(val.split()[0]))) if typ == "xref" else val
            if typ == "dict":
                txt += "\n" + "\n".join(s for r in _REF.findall(val) for s in _xref_strings(doc, int(r)))
            add("form_fields", m.count(txt))

        for name in doc.embfile_names():
            rep.inspected["embedded_files"] += 1
            info = doc.embfile_info(name) or {}
            add("embedded_files", m.count("\n".join([name] + [str(info.get(k, "") or "") for k in
                                                              ("filename", "ufilename", "description", "desc")])))
            try:
                if int(info.get("size", 0) or 0) <= _MAX_EMBED_BYTES:
                    add("embedded_files", m.count(_decode_bytes(doc.embfile_get(name))))
            except Exception:  # noqa: BLE001
                pass

        for entry in doc.get_toc(simple=False):
            rep.inspected["outlines"] += 1
            dest = entry[3] if len(entry) > 3 and isinstance(entry[3], dict) else {}
            s = "\n".join([str(entry[1])] + [str(dest.get(k, "") or "") for k in ("uri", "file", "nameddest", "name")])
            add("outlines", m.count("\n".join((s, unquote(s)))))
        rep.ok = True
    except Exception as exc:  # noqa: BLE001 - malformed structure: inspection incomplete, never "clean"
        rep.ok, rep.reason = False, f"inspect_error:{type(exc).__name__}"
    finally:
        doc.close()
    return rep
