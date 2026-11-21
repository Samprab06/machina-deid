"""Attempt 2c: scoped PDF identity discovery from text geometry (title blocks, contact fields, lookups).

The attempt-1/2 PDF discovery only read ``LABEL: value`` from the plain text stream. Drawing title blocks put
labels and values in separate cells, so the value is a *separate* text line located right of (or below) its
role label. This module reconstructs character-level lines from ``rawdict`` (multi-run, any direction),
associates values to role labels through layout in the label's own text frame (rotated title blocks work the
same way), and also detects structured contact values (email, phone, URL/domain) anywhere on the page.

Findings carry the raw value in memory only (``repr`` is redacted). Nothing here writes files.
"""
from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field

import pymupdf

from .discovery import _plausible, normalize

# ------------------------------------------------------------------ role labels

# role -> (kind, label regex). Order matters only for documentation; matching is whole-label.
ROLE_LABELS: dict[str, tuple[str, str]] = {
    "drawn": ("person", r"drawn(?:\s*by)?|drn|dwn|drafted(?:\s*by)?|draftsman|drawer"),
    "designed": ("person", r"design(?:ed)?(?:\s*by)?|dsgn|dsn|designer"),
    "checked": ("person", r"check(?:ed)?(?:\s*by)?|chk|chkd|ckd|checker"),
    "approved": ("person", r"approv(?:ed|al)?(?:\s*by)?|appd|appr|apvd|app'd"),
    "engineer": ("person", r"engineer|eng(?:ineering)?\s*appr(?:oval)?|resp(?:onsible)?\.?\s*eng(?:ineer)?"),
    "reviewed": ("person", r"review(?:ed)?(?:\s*by)?|verified(?:\s*by)?"),
    "issued": ("person", r"issued\s*by|released\s*by|prepared\s*by|created\s*by|modified\s*by|revised\s*by"),
    "author": ("person", r"author|originator|owner\s*name|contact\s*name|contact|attn|attention"),
    "qa": ("person", r"q\.?a\.?(?:\s*appr(?:oval)?)?|quality|mfg\s*appr(?:oval)?|manufacturing\s*appr(?:oval)?"),
    "company": ("org", r"company(?:\s*name)?|customer|client|supplier|vendor|manufacturer|owner|organi[sz]ation|"
                       r"firm|contractor|consultant|prepared\s*for|bill\s*to|ship\s*to|sold\s*to"),
    "project": ("project", r"project(?:\s*name)?|job(?:\s*name)?|site"),
    "email": ("email", r"e-?mail|mail"),
    "phone": ("phone", r"tel(?:ephone)?|phone|ph|fax|mobile|cell"),
    "address": ("address", r"address|addr"),
}
_LABEL_RES = {role: re.compile(rf"^\s*(?:{rx})\s*[:.\-=#]?\s*$", re.I) for role, (_, rx) in ROLE_LABELS.items()}
_INLINE_RES = {role: re.compile(rf"^\s*(?P<label>{rx})\s*[:=]\s*(?P<value>\S.*)$", re.I)
               for role, (_, rx) in ROLE_LABELS.items()}
_INLINE_PERSON = re.compile(r"^\s*(?P<label>(?:drawn|designed|checked|approved|prepared|created|issued|"
                            r"revised|reviewed|released)\s+by)\s+(?P<value>\S.*)$", re.I)

# Any text that is itself a title-block/engineering label (used to stop value association).
_STRUCTURAL = re.compile(
    r"^\s*(?:date|scale|sheet|size|rev(?:ision)?|dwg\.?\s*no\.?|drawing\s*(?:no|number|title)|title|part\s*(?:no|number)|"
    r"material|finish|weight|mass|units?|tolerances?|unless\s+otherwise|qty|quantity|item|description|"
    r"name|signature|sign|initials?|projection|format|zone|ecn|eco|status|third\s*angle|first\s*angle|"
    r"do\s*not\s*scale|dimensions?|angles?|fractions?|decimals?|treatment|hardness|surface|break|deburr|"
    r"general\s*notes?|notes?|approvals?|revisions?|cage(?:\s*code)?|fscm|contract\s*no\.?|code|"
    r"next\s*assy|used\s*on|application|of|n/?a|tbd|-+|\.+|x+)\s*[:.]?\s*$", re.I)
_PLACEHOLDER = re.compile(r"^\s*(?:n/?a|tbd|tba|none|nil|-+|\.+|_+|x+|\?+|name|signature|date|sign|initials?)\s*$",
                          re.I)

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}\.)+"
                      r"[A-Za-z]{2,24}(?![\w-])")
URL_RE = re.compile(r"(?<![\w@.-])(?:https?://)?(?:www\.)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24}(?:/[^\s]*)?", re.I)
_URL_DOMAIN = re.compile(r"(?:https?://)?(?:www\.)?((?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24})", re.I)
# Phone: needs separators or a leading +, 7..15 digits overall, not a date/dimension/tolerance.
PHONE_RE = re.compile(r"(?<![\w.+/-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,5}\)[\s.-]?)?\d{2,6}(?:[\s.-]\d{2,6}){1,4}(?![\w./-])")
_NAME_TOKEN = re.compile(r"^(?:[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*\.?|[A-Za-z]\.(?:[A-Za-z]\.)*[A-Za-z'’\-]*)$")
_ENGINEERING_WORDS = frozenset(
    "steel aluminum aluminium brass copper bronze stainless plastic nylon delrin abs pvc iron cast alloy mild zinc "
    "chrome nickel titanium rubber ptfe teflon acetal hdpe ldpe glass wood paint anodize anodized plated "
    "assembly assy part detail section view scale sheet rev revision date drawing title material finish weight "
    "metric inch mm in ft deg typ max min nom ref thru hole holes thread bolt nut washer screw pin bracket plate "
    "shaft housing cover base frame tube pipe flange gear bearing spring weld note notes see table bom qty "
    "unless otherwise specified dimensions tolerances angles decimals fractions break edges remove burrs "
    "first third angle projection do not scale approved checked drawn designed company customer".split())


def role_kind(role: str) -> str:
    return ROLE_LABELS[role][0]


def match_label(text: str) -> str | None:
    for role, rx in _LABEL_RES.items():
        if rx.match(text):
            return role
    return None


# ------------------------------------------------------------------ geometry


@dataclass
class Line:
    """One extracted text line with per-character boxes (in ``page.rect`` space)."""
    page: int
    text: str
    boxes: list[tuple[float, float, float, float]] = field(repr=False)
    dir: tuple[float, float]
    size: float
    color: int
    font: str
    origin: tuple[float, float]
    origins: list[tuple[float, float]] = field(default_factory=list, repr=False)

    @property
    def bbox(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.boxes[0])
        for b in self.boxes[1:]:
            r |= b
        return r

    def frame(self, ref: tuple[float, float]) -> tuple[float, float, float, float]:
        """(u0, u1, v0, v1) extents of this line projected on reference direction ``ref`` (u) and its normal."""
        c, s = ref
        us, vs = [], []
        for x0, y0, x1, y1 in self.boxes:
            for x, y in ((x0, y0), (x1, y1), (x0, y1), (x1, y0)):
                us.append(x * c + y * s)
                vs.append(-x * s + y * c)
        return min(us), max(us), min(vs), max(vs)

    def span_rect(self, a: int, b: int) -> pymupdf.Rect:
        r = pymupdf.Rect(self.boxes[a])
        for bx in self.boxes[a + 1:b]:
            r |= bx
        return r


def page_lines(page, pno: int | None = None) -> list[Line]:
    """Character-level lines; spans of one line are joined (multi-run text) in reading order."""
    pno = page.number if pno is None else pno
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES
                        | pymupdf.TEXT_MEDIABOX_CLIP)
    out: list[Line] = []
    for b in raw.get("blocks", []):
        for ln in b.get("lines", []):
            chars, boxes, origins = [], [], []
            size = color = 0
            font = ""
            origin = None
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    c = ch.get("c", "")
                    if not c:
                        continue
                    if origin is None:
                        origin = tuple(ch.get("origin", (0, 0)))
                        size, color, font = sp.get("size", 0), sp.get("color", 0), sp.get("font", "")
                    chars.append(c)
                    boxes.append(tuple(ch["bbox"]))
                    origins.append(tuple(ch.get("origin", ch["bbox"][:2])))
            if not chars or not "".join(chars).strip():
                continue
            d = tuple(ln.get("dir", (1.0, 0.0)))
            out.append(Line(pno, "".join(chars), boxes, d, float(size), int(color), font, origin or (0, 0), origins))
    return out


def _same_dir(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] * b[0] + a[1] * b[1] > 0.98


def _split_lines(lines: list[Line]) -> list[Line]:
    """Split lines on runs of >= 3 spaces or large gaps: one PDF 'line' often spans several table cells."""
    out = []
    for ln in lines:
        start = 0
        n = len(ln.text)
        i = 0
        cuts = []
        while i < n:
            if ln.text[i] == " ":
                j = i
                while j < n and ln.text[j] == " ":
                    j += 1
                if j - i >= 3:
                    cuts.append((i, j))
                i = j
            else:
                if i > 0 and ln.text[i - 1] != " ":
                    gap = _gap(ln, i - 1, i)
                    if gap > max(ln.size, 1.0) * 1.5:
                        cuts.append((i, i))
                i += 1
        for a, b in cuts + [(n, n)]:
            seg = ln.text[start:a]
            lead = len(seg) - len(seg.lstrip())
            trail = len(seg.rstrip())
            if seg.strip():
                s0, s1 = start + lead, start + trail
                extra = {"confs": ln.confs[s0:s1]} if getattr(ln, "confs", None) else {}
                out.append(dataclasses.replace(ln, text=ln.text[s0:s1], boxes=ln.boxes[s0:s1],
                                               origin=ln.origins[s0] if ln.origins else ln.origin,
                                               origins=ln.origins[s0:s1], **extra))
            start = b
    return out


def _gap(ln: Line, i: int, j: int) -> float:
    c, s = ln.dir
    a, b = ln.boxes[i], ln.boxes[j]
    ua = max(a[0] * c + a[1] * s, a[2] * c + a[3] * s, a[0] * c + a[3] * s, a[2] * c + a[1] * s)
    ub = min(b[0] * c + b[1] * s, b[2] * c + b[3] * s, b[0] * c + b[3] * s, b[2] * c + b[1] * s)
    return ub - ua


# ------------------------------------------------------------------ findings


@dataclass
class Finding:
    page: int  # -1 for document-level (metadata etc.)
    kind: str  # person | org | email | phone | domain | project | address
    value: str = field(repr=False)
    role: str = ""
    source: str = ""  # label_right | label_below | inline | pattern | lookup | metadata:<key> | annot | widget
    line: Line | None = field(default=None, repr=False)
    start: int = 0
    end: int = 0
    confidence: str = "medium"

    @property
    def rect(self) -> pymupdf.Rect | None:
        return self.line.span_rect(self.start, self.end) if self.line else None


def _clean_value(v: str) -> str:
    return re.sub(r"\s+", " ", v).strip(" :;,|/\\")


def valid_value(kind: str, value: str) -> bool:
    v = _clean_value(value)
    if not v or _PLACEHOLDER.match(v) or _STRUCTURAL.match(v) or match_label(v):
        return False
    if kind == "email":
        return bool(EMAIL_RE.fullmatch(v))
    if kind == "phone":
        return bool(PHONE_RE.fullmatch(v)) and 7 <= sum(ch.isdigit() for ch in v) <= 15
    if kind == "domain":
        return bool(_URL_DOMAIN.fullmatch(v))
    if _plausible(v, max_len=80):
        return False
    toks = v.split()
    if kind == "person":
        if len(toks) > 4 or not all(_NAME_TOKEN.match(t) for t in toks):
            return False
        letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", v)
        if len(letters) < 2 or all(t.lower().strip(".") in _ENGINEERING_WORDS for t in toks):
            return False
        return True
    if kind in ("org", "project", "address"):
        if len(toks) > 10:
            return False
        if all(t.lower().strip(".,") in _ENGINEERING_WORDS for t in toks):
            return False
        return sum(ch.isalpha() for ch in v) >= 2
    return False


def _strip_span(text: str, a: int, b: int) -> tuple[int, int]:
    while a < b and text[a] in " :;,|/\\":
        a += 1
    while b > a and text[b - 1] in " :;,|/\\":
        b -= 1
    return a, b


def _associate(label: Line, cands: list[Line], max_dist: float) -> tuple[Line, str] | None:
    """Nearest value line right of the label (same text row) else directly below (same column), in the label's
    own text frame. A structural/label line in between stops the search."""
    u0, u1, v0, v1 = label.frame(label.dir)
    h = max(v1 - v0, 1.0)
    right, below = [], []
    for c in cands:
        if c is label or not _same_dir(c.dir, label.dir):
            continue
        cu0, cu1, cv0, cv1 = c.frame(label.dir)
        cmid = (cv0 + cv1) / 2
        if abs(cmid - (v0 + v1) / 2) < 0.6 * h and cu0 >= u1 - 0.5 and cu0 - u1 <= max_dist:
            right.append((cu0 - u1, c))
        elif cv0 >= v1 - 0.3 * h and cv0 - v1 <= 2.2 * h and min(u1, cu1) - max(u0, cu0) > -0.5 * h \
                and cu0 >= u0 - 3 * h:
            below.append((cv0 - v1, c))
    for dist_list, how in ((right, "label_right"), (below, "label_below")):
        dist_list.sort(key=lambda t: t[0])
        if dist_list:
            c = dist_list[0][1]
            if _STRUCTURAL.match(c.text) or match_label(c.text):
                continue
            return c, how
    return None


def scan_page_lines(lines: list[Line]) -> list[Finding]:
    """Role-label + pattern findings on one page's split lines."""
    out: list[Finding] = []
    lines = _split_lines(lines)
    width = max((ln.bbox.x1 for ln in lines), default=0) - min((ln.bbox.x0 for ln in lines), default=0)
    max_dist = max(60.0, min(260.0, width * 0.25))
    taken: set[tuple[int, int, int]] = set()

    def add(f: Finding) -> None:
        key = (id(f.line), f.start, f.end)
        if key not in taken:
            taken.add(key)
            out.append(f)

    for ln in lines:
        role = match_label(ln.text)
        if role:
            got = _associate(ln, lines, max_dist)
            if got:
                c, how = got
                a, b = _strip_span(c.text, 0, len(c.text))
                kind = role_kind(role)
                if kind in ("email", "phone"):
                    continue  # handled by the patterns below (value shape is required)
                if valid_value(kind, c.text[a:b]):
                    add(Finding(ln.page, kind, c.text[a:b], role, how, c, a, b,
                                "high" if how == "label_right" else "medium"))
            continue
        for rx in (_INLINE_PERSON, *_INLINE_RES.values()):
            m = rx.match(ln.text)
            if not m:
                continue
            r = match_label(m.group("label")) or "issued"
            kind = role_kind(r)
            a, b = _strip_span(ln.text, m.start("value"), len(ln.text))
            if kind not in ("email", "phone") and valid_value(kind, ln.text[a:b]):
                add(Finding(ln.page, kind, ln.text[a:b], r, "inline", ln, a, b, "high"))
            break
    for ln in lines:
        for m in EMAIL_RE.finditer(ln.text):
            add(Finding(ln.page, "email", m.group(0), "email", "pattern", ln, m.start(), m.end(), "high"))
        for m in URL_RE.finditer(ln.text):
            add(Finding(ln.page, "domain", m.group(0), "url", "pattern", ln, m.start(), m.end(), "high"))
        for m in PHONE_RE.finditer(ln.text):
            if valid_value("phone", m.group(0)) and _phone_context(ln, lines, m):
                add(Finding(ln.page, "phone", m.group(0), "phone", "pattern", ln, m.start(), m.end(), "high"))
    return out


def _phone_context(ln: Line, lines: list[Line], m: re.Match) -> bool:
    """A phone-shaped number counts only with a leading '+'/'(' or a phone label on the same/left cell."""
    s = m.group(0)
    if s.startswith("+") or s.startswith("("):
        return True
    before = ln.text[:m.start()].lower()
    if re.search(r"\b(?:tel|phone|ph|fax|mobile|cell|t|f|p)\s*[.:]?\s*$", before):
        return True
    for c in lines:
        if c is not ln and match_label(c.text) == "phone":
            if _associate(c, [ln], 260.0):
                return True
    return False


# ------------------------------------------------------------------ lookup (cross-document)

_TOKEN = re.compile(r"[^\s,;:|/\\()\[\]{}<>\"]+")


def ngram_spans(text: str, max_tokens: int = 5):
    """(start, end) spans of 1..max_tokens consecutive tokens (punctuation-delimited)."""
    toks = [(m.start(), m.end()) for m in _TOKEN.finditer(text)]
    for i in range(len(toks)):
        for j in range(i, min(len(toks), i + max_tokens)):
            a, b = toks[i][0], toks[j][1]
            while b > a and text[b - 1] in ".,'’-":
                b -= 1
            if b > a:
                yield a, b


def lookup_findings(lines: list[Line], resolve, *, min_len: int = 4) -> list[Finding]:
    """Exact (normalized) n-gram spans of any line that ``resolve(value)`` knows (frozen entity map/catalog).
    Longest match wins; overlapping shorter spans are dropped."""
    out: list[Finding] = []
    for ln in _split_lines(lines):
        hits = []
        for a, b in ngram_spans(ln.text):
            v = ln.text[a:b]
            if len(normalize(v)) < min_len:
                continue
            got = resolve(v)
            if got:
                hits.append((b - a, a, b, got))
        hits.sort(key=lambda t: (-t[0], t[1]))
        used: list[tuple[int, int]] = []
        for _, a, b, (kind, _ps) in hits:
            if any(a < y and x < b for x, y in used):
                continue
            used.append((a, b))
            out.append(Finding(ln.page, kind, ln.text[a:b], "lookup", "lookup", ln, a, b, "high"))
    return out


def page_is_textless(page) -> dict:
    """Classify a page for the OCR/vector assessment: text chars, image coverage, vector drawing count."""
    text = page.get_text("text").strip()
    area = abs(page.rect) or 1.0
    cover = 0.0
    try:
        for info in page.get_image_info():
            cover += abs(pymupdf.Rect(info["bbox"]) & page.rect)
    except Exception:  # noqa: BLE001
        pass
    try:
        ndraw = len(page.get_drawings())
    except Exception:  # noqa: BLE001
        ndraw = -1
    cls = ("text" if text else "image_scan" if cover / area > 0.5 else "vector_outlined" if ndraw > 50 else
           "empty_or_sparse")
    return {"class": cls, "chars": len(text), "image_cover": round(min(cover / area, 1.0), 3), "drawings": ndraw}


def angle_of(d: tuple[float, float]) -> float:
    return math.degrees(math.atan2(d[1], d[0]))
