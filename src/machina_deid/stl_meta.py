"""Attempt 2b: STL header text (pass-2 lookup of existing approvals only).

* Binary STL: 80-byte header, uint32 facet count, 50 bytes per facet. Only the printable-ASCII header
  text (up to the first NUL) is considered; the file must be exactly ``84 + 50 * count`` bytes.
  Output: same 80-byte header length (original padding byte kept), bytes ``[80:]`` byte-identical.
* ASCII STL: only the name after ``solid`` / ``endsolid`` keywords. Every other line is byte-identical;
  line count, facet/vertex keyword counts and numeric tokens are re-checked.

Replacement is boundary-aware: a span of whole tokens (letters/digits; anything else is a boundary)
whose text is exactly an approved catalog value of kind ``person``/``org`` (lookups via the caller's
``lookup``). Spans of 1..4 tokens, >= 3 characters, at least one letter. Triangles are never touched.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_ASCII_BYTES = 512 * 1024 * 1024
_TOK = re.compile(r"[^\W_]+")
_SOLID = re.compile(rb"^([ \t]*)(solid|endsolid)([ \t]+)([^\r\n]*?)([ \t]*)(\r?\n|$)", re.M | re.I)
_NUM = re.compile(rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[Ee][+-]?\d+)?")
Lookup = Callable[[str], "str | None"]


class StlError(Exception):
    """Reason code only."""


def is_binary(data: bytes) -> bool:
    return len(data) >= 84 and len(data) == 84 + 50 * struct.unpack_from("<I", data, 80)[0]


def header_text(data: bytes) -> tuple[str, int] | None:
    """(text, end offset) of a binary header's printable-ASCII prefix, or None."""
    raw = data[:80].split(b"\x00", 1)[0]
    if not raw or any(b < 0x20 or b > 0x7E for b in raw):
        return None
    return raw.decode("ascii"), len(raw)


def spans(text: str) -> list[tuple[int, int]]:
    toks = [m.span() for m in _TOK.finditer(text)]
    out = []
    for i in range(len(toks)):
        for j in range(i, min(i + 4, len(toks))):
            a, b = toks[i][0], toks[j][1]
            if b - a >= 3 and any(c.isalpha() for c in text[a:b]):
                out.append((a, b))
    return sorted(out, key=lambda s: (-(s[1] - s[0]), s[0]))


def substitute(text: str, lookup: Lookup) -> tuple[str, int]:
    """Longest-first, non-overlapping replacement of approved spans."""
    taken: list[tuple[int, int, str]] = []
    for a, b in spans(text):
        if any(a < y and x < b for x, y, _ in taken):
            continue
        new = lookup(text[a:b])
        if new is not None and new != text[a:b]:
            taken.append((a, b, new))
    out, pos = [], 0
    for a, b, new in sorted(taken):
        out += [text[pos:a], new]
        pos = b
    out.append(text[pos:])
    return "".join(out), len(taken)


def candidates(data: bytes) -> list[str]:
    """Header/solid-name text strings (for discovery / pilot counts)."""
    if is_binary(data):
        h = header_text(data)
        return [h[0]] if h else []
    if not data.lstrip()[:5].lower() == b"solid":
        return []
    return [m.group(4).decode("ascii") for m in _SOLID.finditer(data) if m.group(4) and m.group(4).isascii()]


@dataclass
class StlResult:
    outcome: str = "failed_validation"
    ok: bool = False
    reason: str = ""
    replacements: int = 0
    kind: str = ""  # binary | ascii


def _write(dst: Path, src: Path, out: bytes, sha: str) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:10]}.partial")
    try:
        with tmp.open("wb") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        if hashlib.sha256(src.read_bytes()).hexdigest() != sha:
            return False
        os.replace(tmp, dst)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def transform_stl(src: str | Path, dst: str | Path, lookup: Lookup, *, overwrite=False) -> StlResult:
    src, dst = Path(src), Path(dst)
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    res = StlResult()
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if is_binary(data):
        res.kind = "binary"
        h = header_text(data)
        if h is None:
            res.outcome, res.ok, res.reason = "inspected_no_identifiers", True, "no_text_header"
            return res
        text, end = h
        new, n = substitute(text, lookup)
        if not n:
            res.outcome, res.ok, res.reason = "inspected_no_identifiers", True, "no_exact_header_match"
            return res
        nb = new.encode("ascii", "strict") if new.isascii() else None
        if nb is None or len(nb) > 80:
            res.outcome, res.reason = "identifiers_unmodifiable", "header_overflow_or_non_ascii"
            return res
        pad = data[end:end + 1] if end < 80 else b" "  # keep the exporter's own padding byte (NUL/space)
        if len(nb) <= end:
            header = nb + pad * (end - len(nb)) + data[end:80]
        elif data[end:len(nb)].strip(b"\x00 ") == b"":  # longer text may only overwrite padding
            header = nb + data[len(nb):80]
        else:
            res.outcome, res.reason = "identifiers_unmodifiable", "header_overflow_or_non_ascii"
            return res
        out = header + data[80:]
        if len(out) != len(data) or out[80:] != data[80:] or not is_binary(out):
            res.reason = "binary_body_changed"
            return res
        if header_text(out) is None or substitute(header_text(out)[0], lookup)[1]:
            res.reason = "mapped_original_remains"
            return res
    else:
        res.kind = "ascii"
        if len(data) > MAX_ASCII_BYTES or not data.lstrip()[:5].lower() == b"solid":
            res.reason = "not_ascii_stl"
            return res
        n, pieces, pos = 0, [], 0
        for m in _SOLID.finditer(data):
            name = m.group(4)
            if not name or not name.isascii():
                continue
            new, k = substitute(name.decode("ascii"), lookup)
            if k:
                if not new.isascii() or any(c in new for c in "\r\n"):
                    res.outcome, res.reason = "identifiers_unmodifiable", "name_not_ascii"
                    return res
                pieces += [data[pos:m.start(4)], new.encode("ascii")]
                pos, n = m.end(4), n + k
        if not n:
            res.outcome, res.ok, res.reason = "inspected_no_identifiers", True, "no_exact_solid_name_match"
            return res
        pieces.append(data[pos:])
        out = b"".join(pieces)
        strip = lambda b: _SOLID.sub(b"", b)  # noqa: E731 - everything except solid/endsolid lines
        if (strip(out) != strip(data) or out.count(b"\n") != data.count(b"\n")
                or _NUM.findall(strip(out)) != _NUM.findall(strip(data))
                or len(_SOLID.findall(out)) != len(_SOLID.findall(data))):
            res.reason = "ascii_body_changed"
            return res
        if any(substitute(c, lookup)[1] for c in candidates(out)):
            res.reason = "mapped_original_remains"
            return res
    if not _write(dst, src, out, sha):
        res.reason = "source_changed_during_transform"
        return res
    res.outcome, res.ok, res.replacements = "transformed_validated", True, n
    return res
