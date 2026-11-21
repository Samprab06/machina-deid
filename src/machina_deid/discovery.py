"""Conservative identity *candidate* discovery. Never replaces anything.

Each API returns a list of :class:`Candidate` objects (kind, value, location, confidence,
reason). Candidates are suggestions for a human to review and put into an explicit identity
map; nothing here edits a file. Heuristics deliberately skip dimensions, tolerances, entity
ids, timestamps, numbers and (unless ``include_software=True``) software names.

Privacy: ``repr()``/``to_dict()`` and :func:`report` never contain the raw value. A candidate
is identified by a short SHA-256 of its normalized value (``stable_hash``); pass a secret
``salt`` to make hashes non-guessable (unsalted hashes of short names can be brute-forced).
Raw values are available only via ``Candidate.value`` or ``to_dict(include_value=True)``.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import step_transform as st

CONFIDENCES = ("high", "medium", "low")

_PLACEHOLDERS = {"", "unknown", "none", "n/a", "na", "null", "anonymous", "-", "--", "tbd", "undefined",
                 "unspecified", "not specified", "$", "*", "user", "default", "admin", "administrator"}
_NUMERIC_LIKE = re.compile(r"^[\s\d.,:;/\\+\-±°%()xX*×Ø⌀R]*$")
_TIMESTAMP_LIKE = re.compile(r"\b\d{4}[-/.]\d{2}[-/.]\d{2}\b|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b|\bD:\d{8,}")
_TOLERANCE_LIKE = re.compile(r"[±]|\b\d+(\.\d+)?\s*(mm|in|deg|°)\b|\bM\d+(x\d+(\.\d+)?)?\b|\bØ|⌀", re.I)
_HASH_LIKE = re.compile(r"^(?:[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
_VERSION_LIKE = re.compile(r"^v?\d+([._-]\d+)*$", re.I)
_SOFTWARE = re.compile(r"\b(solidworks|solid edge|autocad|inventor|catia|creo|pro/?engineer|fusion|freecad|"
                       r"onshape|nx|siemens|autodesk|dassault|ptc|st-developer|step tools|pymupdf|mupdf|"
                       r"acrobat|ghostscript|libreoffice|microsoft|word|ezdxf|teigha|oda|bricscad|draftsight)\b", re.I)

_LABELS = (r"drawn(?:\s+by)?|designed(?:\s+by)?|checked(?:\s+by)?|approved(?:\s+by)?|reviewed(?:\s+by)?|"
           r"author|company|customer|client|engineer|owner|supplier|manufacturer|contact|prepared\s+by|"
           r"created\s+by|originator|project\s+name|organi[sz]ation")
_LABEL_RE = re.compile(rf"^\s*(?P<label>{_LABELS})\s*[:\-=]?\s*(?P<rest>.*)$", re.I)
_ATTR_TAGS = re.compile(r"^(?:author|drawn_?by|drawn|designer|designed_?by|checked_?by|approved_?by|company|"
                        r"customer|client|owner|engineer|organi[sz]ation|project_?name|created_?by|supplier)$", re.I)


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def stable_hash(value: str, salt: bytes | None = None, length: int = 12) -> str:
    data = normalize(value).encode("utf-8")
    d = hmac.new(salt, data, hashlib.sha256).hexdigest() if salt else hashlib.sha256(data).hexdigest()
    return d[:length]


@dataclass(frozen=True)
class Candidate:
    kind: str  # person_or_org | file_name | project | text_label_value | path_component | description | ...
    value: str = field(repr=False)
    location: str  # e.g. "step:FILE_NAME.author[0]" - never contains raw values
    confidence: str
    reason: str

    def __repr__(self) -> str:
        return f"Candidate(kind={self.kind!r}, location={self.location!r}, confidence={self.confidence!r}, hash={stable_hash(self.value)})"

    def to_dict(self, *, include_value: bool = False, salt: bytes | None = None) -> dict:
        d = {"kind": self.kind, "location": self.location, "confidence": self.confidence, "reason": self.reason,
             "hash": stable_hash(self.value, salt), "length": len(self.value)}
        if include_value:
            d["value"] = self.value
        return d


def report(cands: list[Candidate], *, salt: bytes | None = None, include_values: bool = False) -> dict:
    """Aggregate report. Contains hashes, never raw values (unless explicitly requested)."""
    by_kind: dict[str, int] = {}
    for c in cands:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    uniq = {stable_hash(c.value, salt) for c in cands}
    return {"candidates": len(cands), "unique_values": len(uniq), "by_kind": dict(sorted(by_kind.items())),
            "items": [c.to_dict(include_value=include_values, salt=salt) for c in cands]}


# ---------------------------------------------------------------- filters


def _plausible(value: str, *, min_len: int = 2, max_len: int = 120) -> str | None:
    """Return a rejection reason code or None if ``value`` may be an identity."""
    v = value.strip()
    if normalize(v) in _PLACEHOLDERS:
        return "placeholder"
    if len(v) < min_len or len(v) > max_len:
        return "length"
    if not any(ch.isalpha() for ch in v):
        return "no_letters"
    if _NUMERIC_LIKE.match(v):
        return "numeric"
    if _TIMESTAMP_LIKE.search(v):
        return "timestamp"
    if _TOLERANCE_LIKE.search(v) and sum(ch.isdigit() for ch in v) * 3 >= len(v):
        return "dimension_or_tolerance"
    if _HASH_LIKE.match(v) or _VERSION_LIKE.match(v):
        return "hash_or_version"
    if sum(ch.isdigit() for ch in v) * 2 > len(v):
        return "mostly_digits"
    return None


def _mk(out: list[Candidate], kind: str, value: str, location: str, confidence: str, reason: str,
        include_software: bool = False) -> None:
    v = value.strip()
    if _plausible(v):
        return
    if not include_software and _SOFTWARE.search(v):
        return
    out.append(Candidate(kind, v, location, confidence, reason))


# ---------------------------------------------------------------- STEP


def discover_step(path: str | Path, *, include_software: bool = False, include_timestamps: bool = False,
                  include_description: bool = True) -> list[Candidate]:
    """Header fields of an ISO-10303-21 file. DATA is never read."""
    hdr, _ = st.read_step_header(path)
    out: list[Candidate] = []
    counters: dict[str, int] = {}
    for s in hdr.strings:
        raw = hdr.text[s.start + 1:s.end - 1]
        text, directives = st._split_content(raw)
        if directives:
            text = st._decode_directives(text)
        if s.field is None:
            continue
        i = counters[s.field] = counters.get(s.field, -1) + 1
        loc = f"step:FILE_NAME.{s.field}[{i}]"
        if s.field == "name":
            _mk(out, "file_name", text, loc, "medium", "FILE_NAME name often embeds a project/part/person name")
        elif s.field in ("author", "organization"):
            _mk(out, "person_or_org", text, loc, "high", f"FILE_NAME {s.field} tuple entry")
        elif s.field in ("originating_system", "authorization", "preprocessor"):
            if include_software or s.field == "authorization":
                _mk(out, "software_or_authorization", text, loc, "low", f"FILE_NAME {s.field}", include_software)
        elif s.field == "timestamp" and include_timestamps:
            out.append(Candidate("timestamp", text, loc, "low", "explicitly requested timestamp"))
    if include_description:
        d = next((r for r in hdr.records if r.keyword == "FILE_DESCRIPTION"), None)
        if d is not None:
            for k, s in enumerate(st._flatten_strings(d.params[0] if d.params else [])):
                text, directives = st._split_content(hdr.text[s.start + 1:s.end - 1])
                if not directives:
                    _mk(out, "description", text, f"step:FILE_DESCRIPTION.description[{k}]", "low",
                        "free-text description; review manually")
    return out


# ---------------------------------------------------------------- PDF


def _label_values(lines: list[str]):
    """Yield (label, value) for 'LABEL: value' lines or LABEL on one line and value on the next."""
    for i, line in enumerate(lines):
        m = _LABEL_RE.match(line)
        if not m:
            continue
        rest = m.group("rest").strip()
        if rest:
            yield m.group("label"), rest
        elif i + 1 < len(lines) and not _LABEL_RE.match(lines[i + 1]):
            yield m.group("label"), lines[i + 1].strip()


def discover_pdf(path: str | Path, *, include_software: bool = False, max_pages: int = 3) -> list[Candidate]:
    import pymupdf

    out: list[Candidate] = []
    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            return out
        meta = doc.metadata or {}
        for key, kind, conf in (("author", "person_or_org", "high"), ("title", "file_name", "medium"),
                                ("subject", "description", "low"), ("keywords", "description", "low")):
            if meta.get(key):
                _mk(out, kind, meta[key], f"pdf:metadata.{key}", conf, f"PDF Info {key}")
        if include_software:
            for key in ("creator", "producer"):
                if meta.get(key):
                    _mk(out, "software_or_authorization", meta[key], f"pdf:metadata.{key}", "low",
                        f"PDF Info {key}", True)
        for pno in range(min(doc.page_count, max_pages)):
            lines = [ln.strip() for ln in doc[pno].get_text("text").splitlines() if ln.strip()]
            for n, (label, value) in enumerate(_label_values(lines)):
                _mk(out, "text_label_value", value, f"pdf:page[{pno}].title_block[{n}]", "medium",
                    "value following a title-block label")
    finally:
        doc.close()
    return out


# ---------------------------------------------------------------- DXF

_HEADER_VARS = (("$LASTSAVEDBY", "person_or_org", "high"), ("$PROJECTNAME", "project", "medium"))


def discover_dxf(path: str | Path, *, include_software: bool = False) -> list[Candidate]:
    """Editable DXF text: header string vars, custom properties, ATTRIB/ATTDEF with identity-like tags,
    and TEXT/MTEXT following a title-block label. DIMENSION text is never inspected."""
    import ezdxf

    out: list[Candidate] = []
    doc = ezdxf.readfile(path)
    for var, kind, conf in _HEADER_VARS:
        if var in doc.header and isinstance(doc.header[var], str):
            _mk(out, kind, doc.header[var], f"dxf:header.{var}", conf, f"DXF header {var}")
    for n, (tag, value) in enumerate(doc.header.custom_vars):
        _mk(out, "text_label_value", value, f"dxf:header.custom[{n}]", "medium", "DXF custom header property")
    n = 0
    for space in list(doc.layouts) + list(doc.blocks):
        for e in space:
            t = e.dxftype()
            ents = [e] + (list(e.attribs) if t == "INSERT" else [])
            for x in ents:
                xt = x.dxftype()
                if xt in ("ATTRIB", "ATTDEF"):
                    tag = x.dxf.get("tag", "")
                    val = x.dxf.get("text", "")
                    if _ATTR_TAGS.match(tag or ""):
                        _mk(out, "text_label_value", val, f"dxf:attrib[{n}]", "high",
                            "attribute with identity-like tag")
                    n += 1
                elif xt in ("TEXT", "MTEXT"):
                    text = x.plain_text() if xt == "MTEXT" else x.dxf.get("text", "")
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    for label, value in _label_values(lines):
                        _mk(out, "text_label_value", value, f"dxf:text[{n}]", "medium",
                            "value following a title-block label")
                    n += 1
    return out


# ---------------------------------------------------------------- paths

_GENERIC_COMPONENTS = {"input", "inputs", "sources", "source", "catalog", "corpus", "data", "files", "cad",
                       "extracted", "docs", "drawings", "models", "parts", "assembly", "archive", "archives",
                       "output", "tmp", "temp", "src"}


def discover_path(rel_path: str) -> list[Candidate]:
    """Each directory component and the file stem of a manifest-relative path (low confidence)."""
    p = PurePosixPath(rel_path.replace("\\", "/"))
    parts = list(p.parts)
    out: list[Candidate] = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        name = PurePosixPath(part).stem if last else part
        name = re.sub(r"\.extracted$", "", name)
        if normalize(name) in _GENERIC_COMPONENTS:
            continue
        loc = "path:file_stem" if last else f"path:dir[{i}]"
        _mk(out, "path_component", name, loc, "low", "path component; may embed a company/person/project name")
    return out
