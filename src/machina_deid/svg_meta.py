"""Attempt 2b: SVG Dublin Core / RDF agent metadata (pass-2 lookup of existing approvals only).

Scope (W3C SVG 1.1 §21 metadata, Dublin Core elements 1.1 ``http://purl.org/dc/elements/1.1/``, the
Creative Commons ``cc:Agent`` pattern written by Inkscape and others): the *text content* of

    dc:creator | dc:publisher | dc:contributor | dc:rights   -- either direct text, or
    dc:<that>/cc:Agent/dc:title                               -- the agent's name

A text node is replaced only when its whole trimmed value is *exactly* an approved catalog value
(person/org kinds). Nothing else is edited: no attribute, path, style, geometry, ``<title>``/``<desc>``.
The output is the input with only those text spans spliced, then verified by reparsing both documents
and comparing every element (tag, attributes, text, tail) except the planned text nodes.
Files declaring a DTD/entities, not UTF-8, or whose regex spans disagree with the parser are refused.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, unescape

from .discovery import Candidate

DC = "http://purl.org/dc/elements/1.1/"
AGENT_FIELDS = ("creator", "publisher", "contributor", "rights")
MAX_BYTES = 64 * 1024 * 1024
_SVG = "{http://www.w3.org/2000/svg}svg"


class SvgError(Exception):
    """Reason code only."""


@dataclass
class _Site:
    start: int
    end: int  # text content span (between tags) in the decoded document
    value: str
    field: str


def _load(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise SvgError("file_too_large")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SvgError("not_utf8") from None
    m = re.match(r"\s*<\?xml[^>]*encoding=[\"']([^\"']+)", text)
    if m and m.group(1).lower().replace("_", "-") not in ("utf-8", "utf8", "us-ascii", "ascii"):
        raise SvgError("declared_non_utf8")
    if "<!DOCTYPE" in text[:4096].upper() and ("<!ENTITY" in text.upper()):
        raise SvgError("entities_declared")
    return text


def _parse(text: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise SvgError("xml_parse_error") from None
    if root.tag != _SVG:
        raise SvgError("not_svg_root")
    return root


def _tree_sites(root: ET.Element) -> list[tuple[str, str]]:
    out = []
    for f in AGENT_FIELDS:
        for el in root.iter(f"{{{DC}}}{f}"):
            kids = list(el)
            if not kids:
                if el.text and el.text.strip():
                    out.append((f, el.text))
                continue
            for t in el.iter(f"{{{DC}}}title"):
                if t.text and t.text.strip() and not list(t):
                    out.append((f, t.text))
    return out


def _dc_prefixes(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"xmlns:([A-Za-z_][\w.\-]*)\s*=\s*[\"']" + re.escape(DC) + r"[\"']", text)}


def _text_sites(text: str) -> list[_Site]:
    sites: list[_Site] = []
    for px in _dc_prefixes(text):
        q = re.escape(px)
        blk = re.compile(rf"<{q}:({'|'.join(AGENT_FIELDS)})\b[^>]*?(?<!/)>(.*?)</{q}:\1\s*>", re.S)
        for m in blk.finditer(text):
            f, inner, base = m.group(1), m.group(2), m.start(2)
            if "<" not in inner:
                if inner.strip():
                    sites.append(_Site(base, base + len(inner), inner, f))
                continue
            for t in re.finditer(rf"<{q}:title\b[^>]*?(?<!/)>([^<]*)</{q}:title\s*>", inner):
                if t.group(1).strip():
                    sites.append(_Site(base + t.start(1), base + t.end(1), t.group(1), f))
    sites.sort(key=lambda s: s.start)
    return sites


def _sites(text: str) -> list[_Site]:
    root = _parse(text)
    sites = _text_sites(text)
    if "<![CDATA[" in text and sites:
        raise SvgError("cdata_present")
    if sorted((s.field, unescape(s.value)) for s in sites) != sorted(_tree_sites(root)):
        raise SvgError("span_parser_disagreement")
    return sites


def discover_svg(path: str | Path) -> list[Candidate]:
    text = _load(Path(path).read_bytes())
    out = []
    for k, s in enumerate(_sites(text)):
        v = unescape(s.value).strip()
        if v:
            out.append(Candidate("person_or_org", v, f"svg:metadata.dc.{s.field}[{k}]", "medium",
                                 "SVG Dublin Core agent; pass-2 lookup of existing approvals only"))
    return out


@dataclass
class SvgResult:
    outcome: str = "failed_validation"
    ok: bool = False
    reason: str = ""
    replacements: int = 0
    residual: int = 0
    sites: int = 0
    by_field: dict[str, int] = field(default_factory=dict)


def transform_svg(src: str | Path, dst: str | Path, value_map: dict[str, str], *, overwrite=False) -> SvgResult:
    """``value_map``: exact trimmed field value -> pseudonym (already approved by the caller)."""
    src, dst = Path(src), Path(dst)
    res = SvgResult()
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    try:
        text = _load(data)
        sites = _sites(text)
    except SvgError as exc:
        res.reason = str(exc)
        return res
    res.sites = len(sites)
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    pieces, pos, edits = [], 0, []
    for s in sites:
        raw = unescape(s.value)
        v = raw.strip()
        if v not in value_map:
            continue
        lead, trail = raw[: len(raw) - len(raw.lstrip())], raw[len(raw.rstrip()):]
        new = escape(lead + value_map[v] + trail)
        pieces += [text[pos:s.start], new]
        pos = s.end
        edits.append((s, lead + value_map[v] + trail))
        res.by_field[s.field] = res.by_field.get(s.field, 0) + 1
    if not edits:
        res.outcome, res.ok, res.reason = "inspected_no_identifiers", True, "no_exact_metadata_match"
        return res
    pieces.append(text[pos:])
    out_text = "".join(pieces)
    # verification: same element tree except the planned text nodes; outside-span bytes identical
    try:
        a, b = _parse(text), _parse(out_text)
    except SvgError:
        res.reason = "output_not_parseable"
        return res
    want = sorted(_tree_sites(a))
    got = sorted(_tree_sites(b))
    if len(want) != len(got):
        res.reason = "site_count_changed"
        return res
    ea, eb = list(a.iter()), list(b.iter())
    if len(ea) != len(eb):
        res.reason = "element_count_changed"
        return res
    new_texts = {e[1] for e in edits}
    for x, y in zip(ea, eb):
        if x.tag != y.tag or x.attrib != y.attrib or (x.tail or "") != (y.tail or ""):
            res.reason = "structure_changed"
            return res
        if (x.text or "") != (y.text or "") and (y.text not in new_texts or x.tag not in
                                                    {f"{{{DC}}}title", *(f"{{{DC}}}{f}" for f in AGENT_FIELDS)}):
            res.reason = "unplanned_text_changed"
            return res
    keep = {k for k in value_map if k not in value_map.values()}
    if any(v.strip() in keep for _, v in got):
        res.reason = "mapped_original_remains"
        return res
    out = bom + out_text.encode("utf-8")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:10]}.partial")
    try:
        with tmp.open("wb") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        if hashlib.sha256(src.read_bytes()).hexdigest() != sha:
            res.reason = "source_changed_during_transform"
            return res
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    res.outcome, res.ok, res.replacements = "transformed_validated", True, len(edits)
    return res
