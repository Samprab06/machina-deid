"""Conservative, structure-aware ISO-10303-21 (STEP) header transformer.

Only *explicitly mapped* strings (``IdentityMap.strings``) are replaced, and only inside
string literals of selected ``FILE_NAME`` header parameters (name, author tuple,
organization tuple and, when mapped, originating_system / authorisation; timestamp and
preprocessor only if requested through ``fields``). Everything else is preserved byte for
byte: the DATA section (never even decoded), entity ids, numbers, references,
FILE_DESCRIPTION, FILE_SCHEMA, comments, whitespace, line endings and encoding.

Outcomes (``StepResult.outcome``):

``transformed_validated``    output written atomically and re-verified structurally
``inspected_no_identifiers`` header is valid but contains no explicit map match; nothing written
``identifiers_unmodifiable`` a match exists but cannot be replaced safely; nothing written
``failed_validation``        malformed/unsafe file or the output failed verification; nothing written

Result objects never contain raw identity values; maps are referred to by index
(insertion order of ``IdentityMap.strings``), like the PDF/DXF transformers.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .idmap import IdentityMap

HEADER_LIMIT = 4 * 1024 * 1024
TAIL_BYTES = 64 * 1024
CHUNK = 1 << 20

FILE_NAME_FIELDS = ("name", "timestamp", "author", "organization", "preprocessor", "originating_system",
                    "authorization")
DEFAULT_FIELDS = ("name", "author", "organization", "originating_system", "authorization")
# Attempt 2b (pass-2 pipeline): ``preprocessor_version`` is free informational text (ISO 10303-21 §8.2.2);
# exporters frequently repeat the organization/user there. Approved values found there were previously
# counted as residual ("mapped_present_in_unrequested_fields") but never replaced. ``time_stamp`` stays
# excluded (never edited; a hit there is still reported).
PASS2_FIELDS = DEFAULT_FIELDS + ("preprocessor",)

OUTCOMES = ("transformed_validated", "inspected_no_identifiers", "unsupported_passthrough",
            "identifiers_unmodifiable", "failed_validation")


class StepError(Exception):
    """Malformed or unsafe STEP structure (message is a short reason code, never content)."""


# ---------------------------------------------------------------- parsing


@dataclass
class _Str:
    start: int  # offset of opening quote
    end: int  # offset just past closing quote
    field: str | None = None  # FILE_NAME field name when inside FILE_NAME


@dataclass
class _Rec:
    keyword: str
    params: list  # nodes: _Str | list | str (atom)


@dataclass
class StepHeader:
    header_end: int  # offset of the DATA keyword: bytes [header_end:] are the untouched DATA section
    records: list[_Rec]
    strings: list[_Str]  # all string literals in the header in file order
    text: str  # latin-1 view of bytes [0:header_end]

    def file_name(self) -> _Rec:
        return next(r for r in self.records if r.keyword == "FILE_NAME")


_WS = " \t\r\n\f\v"
_ATOM_END = set(" \t\r\n\f\v(),;'/")


class _Lexer:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def skip(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in _WS:
                self.i += 1
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j < 0:
                    raise StepError("unterminated_comment")
                self.i = j + 2
            else:
                return

    def peek(self) -> str:
        self.skip()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise StepError("unexpected_token")
        self.i += 1

    def word(self) -> str:
        self.skip()
        j = self.i
        s = self.s
        while j < len(s) and s[j] not in _ATOM_END:
            j += 1
        if j == self.i:
            raise StepError("unexpected_token")
        w, self.i = s[self.i:j], j
        return w

    def string(self) -> tuple[int, int]:
        self.skip()
        s = self.s
        if self.i >= len(s) or s[self.i] != "'":
            raise StepError("unexpected_token")
        start, j = self.i, self.i + 1
        while True:
            k = s.find("'", j)
            if k < 0:
                raise StepError("unterminated_string")
            if k + 1 < len(s) and s[k + 1] == "'":
                j = k + 2
                continue
            self.i = k + 1
            return start, k + 1


def _parse_value(lx: _Lexer, strings: list[_Str], depth: int = 0):
    if depth > 8:
        raise StepError("nesting_too_deep")
    c = lx.peek()
    if c == "'":
        st, en = lx.string()
        node = _Str(st, en)
        strings.append(node)
        return node
    if c == "(":
        lx.i += 1
        items = []
        if lx.peek() == ")":
            lx.i += 1
            return items
        while True:
            items.append(_parse_value(lx, strings, depth + 1))
            c = lx.peek()
            if c == ",":
                lx.i += 1
            elif c == ")":
                lx.i += 1
                return items
            else:
                raise StepError("unexpected_token")
    if c in (")", ",", ";", ""):
        raise StepError("unexpected_token")
    w = lx.word()
    if lx.i < len(lx.s) and lx.s[lx.i] == "(":  # typed parameter, e.g. LENGTH_MEASURE(1.)
        return ("typed", w, _parse_value(lx, strings, depth + 1))
    return w


def _flatten_strings(node):
    if isinstance(node, _Str):
        yield node
    elif isinstance(node, list):
        for n in node:
            yield from _flatten_strings(n)
    elif isinstance(node, tuple):
        yield from _flatten_strings(node[2])


def parse_step_bytes(buf: bytes, *, tail: bytes | None = None) -> StepHeader:
    """Parse the header of ``buf`` (must contain everything up to the DATA keyword).

    ``tail`` (default: end of ``buf``) must end with ``END-ISO-10303-21;``.
    """
    text = buf.decode("latin-1")  # 1:1 byte/offset mapping
    lx = _Lexer(text)
    if lx.word() != "ISO-10303-21":
        raise StepError("missing_iso_10303_21_magic")
    lx.expect(";")
    if lx.word() != "HEADER":
        raise StepError("missing_header_section")
    lx.expect(";")
    records: list[_Rec] = []
    strings: list[_Str] = []
    while True:
        kw = lx.word()
        if kw == "ENDSEC":
            lx.expect(";")
            break
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", kw):
            raise StepError("bad_header_keyword")
        lx.expect("(")
        params: list = []
        if lx.peek() == ")":
            lx.i += 1
        else:
            while True:
                params.append(_parse_value(lx, strings))
                c = lx.peek()
                if c == ",":
                    lx.i += 1
                elif c == ")":
                    lx.i += 1
                    break
                else:
                    raise StepError("unexpected_token")
        lx.expect(";")
        records.append(_Rec(kw.upper(), params))
    lx.skip()
    header_end = lx.i
    if lx.word().upper() != "DATA":
        raise StepError("missing_data_section")
    c = lx.peek()
    if c not in (";", "("):
        raise StepError("bad_data_section_start")
    kws = [r.keyword for r in records]
    for need in ("FILE_DESCRIPTION", "FILE_NAME", "FILE_SCHEMA"):
        if kws.count(need) != 1:
            raise StepError("missing_or_duplicate_" + need.lower())
    fn = next(r for r in records if r.keyword == "FILE_NAME")
    if len(fn.params) != 7:
        raise StepError("file_name_arity")
    for fname, p in zip(FILE_NAME_FIELDS, fn.params):
        for s in _flatten_strings(p):
            s.field = fname
    end_marker = (tail if tail is not None else buf)
    tl = end_marker.decode("latin-1")
    tl = re.sub(r"/\*.*?\*/", "", tl, flags=re.S).rstrip(_WS)
    if not tl.upper().endswith("END-ISO-10303-21;"):
        raise StepError("missing_end_marker")
    return StepHeader(header_end, records, strings, text[:header_end])


def read_step_header(path: str | Path) -> tuple[StepHeader, bytes]:
    """Read only the header region (bounded) and the tail of ``path``. Returns (header, header bytes)."""
    p = Path(path)
    with p.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(HEADER_LIMIT)
        f.seek(max(0, size - TAIL_BYTES))
        tail = f.read(TAIL_BYTES)
    if not head.lstrip().upper().startswith(b"ISO-10303-21"):
        raise StepError("missing_iso_10303_21_magic")
    try:
        hdr = parse_step_bytes(head, tail=tail)
    except StepError as exc:
        if str(exc) in ("missing_data_section", "unexpected_token") and len(head) == HEADER_LIMIT:
            raise StepError("header_too_large") from None
        raise
    return hdr, head[: hdr.header_end]


# ---------------------------------------------------------------- string decoding / matching

_DIRECTIVE = re.compile(r"\\(?:X2\\|X4\\|X\\|S\\|P[A-I]\\|N\\)")


def _split_content(raw: str) -> tuple[str, bool]:
    """Return (logical text, has_directives) for the raw content between the quotes."""
    out: list[str] = []
    directives = False
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "'" and raw[i:i + 2] == "''":
            out.append("'")
            i += 2
        elif c == "\\":
            if raw[i:i + 2] == "\\\\":
                out.append("\\")
                i += 2
            else:
                directives = True
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out), directives


def _decode_directives(text: str) -> str:
    """Best-effort decode of \\X\\HH, \\X2\\..\\X0\\, \\X4\\..\\X0\\ and \\S\\c for matching only."""
    def x2(m):
        h = m.group(1)
        try:
            return bytes.fromhex(h).decode("utf-16-be")
        except ValueError:
            return m.group(0)

    def x4(m):
        try:
            return bytes.fromhex(m.group(1)).decode("utf-32-be")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)

    text = re.sub(r"\\X2\\((?:[0-9A-Fa-f]{4})+)\\X0\\", x2, text)
    text = re.sub(r"\\X4\\((?:[0-9A-Fa-f]{8})+)\\X0\\", x4, text)
    text = re.sub(r"\\X\\([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\\S\\(.)", lambda m: chr(ord(m.group(1)) + 128 & 0xFF), text)
    return text


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "''")


def _variants(old: str, new: str) -> list[tuple[str, str | None]]:
    """(old, new) pairs as they would appear in the latin-1 view of the file.

    ``new`` is None when the replacement cannot be represented in that raw encoding.
    """
    if old.isascii():
        return [(old, new if _safe_new(new) else None)]
    out: list[tuple[str, str | None]] = []
    for enc in ("utf-8", "cp1252"):
        try:
            o = old.encode(enc).decode("latin-1")
        except UnicodeEncodeError:
            continue
        try:
            nw = new.encode(enc).decode("latin-1")
        except UnicodeEncodeError:
            nw = None
        if nw is not None and not _safe_new(new, ascii_only=False):
            nw = None
        out.append((o, nw))
    return out


def _safe_new(new: str, ascii_only: bool = True) -> bool:
    if not new or any(ord(c) < 0x20 or ord(c) == 0x7F for c in new):
        return False
    return new.isascii() if ascii_only else True


@dataclass
class _Edit:
    start: int
    end: int
    new_raw: str  # replacement for [start:end] (including quotes)
    field: str
    counts: dict[int, int]
    expected_text: str  # logical text after replacement


@dataclass
class StepPlan:
    edits: list[_Edit] = field(default_factory=list)
    unmodifiable: dict[int, int] = field(default_factory=dict)  # idx -> occurrences that cannot be replaced
    matches_by_field: dict[str, dict[int, int]] = field(default_factory=dict)  # field -> idx -> n
    skipped_fields_with_matches: dict[str, int] = field(default_factory=dict)  # field -> count (not requested)


def plan_step(hdr: StepHeader, idmap: IdentityMap, fields: tuple[str, ...] = DEFAULT_FIELDS) -> StepPlan:
    for f in fields:
        if f not in FILE_NAME_FIELDS:
            raise ValueError("unknown FILE_NAME field")
    index = {old: i for i, old in enumerate(idmap.strings)}
    table: dict[str, tuple[int, str | None]] = {}
    for old, new in idmap.strings.items():
        for o, n in _variants(old, new):
            table[o] = (index[old], n)
    plan = StepPlan()
    if not table:
        return plan
    pat = re.compile("|".join(re.escape(k) for k in sorted(table, key=lambda k: -len(k))))
    for s in hdr.strings:
        if s.field is None:
            continue
        raw = hdr.text[s.start + 1:s.end - 1]
        text, directives = _split_content(raw)
        if directives:
            text = _decode_directives(text)
        hits = list(pat.finditer(text))
        if not hits:
            continue
        if s.field not in fields:
            plan.skipped_fields_with_matches[s.field] = plan.skipped_fields_with_matches.get(s.field, 0) + len(hits)
            continue
        by = plan.matches_by_field.setdefault(s.field, {})
        for m in hits:
            idx = table[m.group(0)][0]
            by[idx] = by.get(idx, 0) + 1
        bad = [m for m in hits if table[m.group(0)][1] is None]
        if directives or bad:
            for m in (hits if directives else bad):
                idx = table[m.group(0)][0]
                plan.unmodifiable[idx] = plan.unmodifiable.get(idx, 0) + 1
            continue
        counts: dict[int, int] = {}

        def sub(m):
            idx, new = table[m.group(0)]
            counts[idx] = counts.get(idx, 0) + 1
            return new

        new_text = pat.sub(sub, text)
        plan.edits.append(_Edit(s.start, s.end, "'" + _escape(new_text) + "'", s.field, counts, new_text))
    return plan


# ---------------------------------------------------------------- result


@dataclass
class StepResult:
    outcome: str = "failed_validation"
    ok: bool = False
    reason: str = ""
    output_written: bool = False
    source_sha256: str = ""
    output_sha256: str = ""
    input_bytes: int = 0
    output_bytes: int = 0
    data_section_identical: bool = False
    data_bytes: int = 0
    header_fields_changed: dict[str, int] = field(default_factory=dict)  # field -> strings changed
    replacements: dict[int, int] = field(default_factory=dict)  # identity index -> replacements
    unmodifiable: dict[int, int] = field(default_factory=dict)  # identity index -> occurrences
    mapped_present_in_unrequested_fields: dict[str, int] = field(default_factory=dict)
    newline_style: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _newline_style(head: bytes) -> str:
    crlf, lf = head.count(b"\r\n"), head.count(b"\n")
    if lf == 0:
        return "none"
    return "crlf" if crlf == lf else ("lf" if crlf == 0 else "mixed")


def _fail(res: StepResult, reason: str) -> StepResult:
    res.outcome, res.ok, res.reason, res.output_written = "failed_validation", False, reason, False
    return res


def _hash_range(path: Path, start: int) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with path.open("rb") as f:
        f.seek(start)
        while b := f.read(CHUNK):
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def sha256_file(path: Path) -> str:
    return _hash_range(path, 0)[0]


def _shape(node):
    if isinstance(node, _Str):
        return "s"
    if isinstance(node, list):
        return [_shape(n) for n in node]
    if isinstance(node, tuple):
        return ("t", node[1], _shape(node[2]))
    return node


def inspect_step(src: str | Path, idmap: IdentityMap, fields: tuple[str, ...] = DEFAULT_FIELDS) -> StepResult:
    """Read-only: classify the header against the map. Never writes."""
    res = StepResult()
    src = Path(src)
    try:
        res.input_bytes = src.stat().st_size
        hdr, head = read_step_header(src)
        plan = plan_step(hdr, idmap, fields)
    except (StepError, OSError) as exc:
        return _fail(res, str(exc) if isinstance(exc, StepError) else "io_error")
    res.newline_style = _newline_style(head)
    res.mapped_present_in_unrequested_fields = dict(plan.skipped_fields_with_matches)
    res.unmodifiable = dict(plan.unmodifiable)
    for f, by in plan.matches_by_field.items():
        res.header_fields_changed[f] = sum(by.values())
    if plan.unmodifiable:
        res.header_fields_changed = {}
        res.outcome, res.reason = "identifiers_unmodifiable", "explicit_match_not_safely_replaceable"
    elif not plan.edits:
        res.outcome, res.ok, res.reason = "inspected_no_identifiers", True, "no_explicit_header_match"
    else:
        res.outcome, res.reason = "transformed_validated", "plan_only"
    return res


def transform_step(src: str | Path, dst: str | Path, idmap: IdentityMap, *,
                   fields: tuple[str, ...] = DEFAULT_FIELDS, overwrite: bool = False) -> StepResult:
    """Replace explicitly mapped identities in FILE_NAME header strings; write ``dst`` atomically.

    ``dst`` is created only for outcome ``transformed_validated``.
    """
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("output path must differ from source path")
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    res = StepResult()
    try:
        res.input_bytes = src.stat().st_size
        hdr, head = read_step_header(src)
        plan = plan_step(hdr, idmap, fields)
    except StepError as exc:
        return _fail(res, str(exc))
    except OSError:
        return _fail(res, "io_error")
    res.newline_style = _newline_style(head)
    res.mapped_present_in_unrequested_fields = dict(plan.skipped_fields_with_matches)
    if plan.unmodifiable:
        res.outcome, res.reason = "identifiers_unmodifiable", "explicit_match_not_safely_replaceable"
        res.unmodifiable = dict(plan.unmodifiable)
        res.source_sha256 = sha256_file(src)
        return res
    if not plan.edits:
        res.outcome, res.ok, res.reason = "inspected_no_identifiers", True, "no_explicit_header_match"
        res.source_sha256 = sha256_file(src)
        return res

    # build the new header by splicing only the edited string literals
    pieces, pos = [], 0
    for e in sorted(plan.edits, key=lambda e: e.start):
        pieces.append(head[pos:e.start])
        pieces.append(e.new_raw.encode("latin-1"))
        pos = e.end
    pieces.append(head[pos:])
    new_head = b"".join(pieces)

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:10]}.partial")
    try:
        src_h, data_h, out_h = hashlib.sha256(head), hashlib.sha256(), hashlib.sha256(new_head)
        with src.open("rb") as i, tmp.open("wb") as o:
            o.write(new_head)
            i.seek(len(head))
            while b := i.read(CHUNK):
                src_h.update(b)
                data_h.update(b)
                out_h.update(b)
                o.write(b)
            o.flush()
            os.fsync(o.fileno())
        res.source_sha256 = src_h.hexdigest()
        if src.stat().st_size != res.input_bytes:
            return _fail(res, "source_changed_during_read")
        r = _verify(tmp, hdr, plan, idmap, len(new_head), data_h.hexdigest(), out_h.hexdigest(), res)
        if r:
            return _fail(res, r)
        if dst.exists() and not overwrite:
            return _fail(res, "output_exists")
        os.replace(tmp, dst)
        res.outcome, res.ok, res.reason, res.output_written = "transformed_validated", True, "", True
        res.output_sha256 = out_h.hexdigest()
        res.output_bytes = dst.stat().st_size
        for e in plan.edits:
            res.header_fields_changed[e.field] = res.header_fields_changed.get(e.field, 0) + 1
            for idx, n in e.counts.items():
                res.replacements[idx] = res.replacements.get(idx, 0) + n
        return res
    finally:
        if tmp.exists():
            tmp.unlink()


def _verify(tmp: Path, hdr: StepHeader, plan: StepPlan, idmap: IdentityMap, new_head_len: int,
            data_sha: str, out_sha: str, res: StepResult) -> str:
    """Reopen ``tmp`` and verify. Returns a failure reason or '' when everything holds."""
    try:
        new_hdr, new_head = read_step_header(tmp)
    except (StepError, OSError):
        return "output_not_parseable"
    if new_hdr.header_end != new_head_len:
        return "header_boundary_moved"
    got, n = _hash_range(tmp, new_hdr.header_end)
    res.data_bytes = n
    if got != data_sha:
        return "data_section_changed"
    res.data_section_identical = True
    if sha256_file(tmp) != out_sha:
        return "output_checksum_mismatch"
    if [_shape(r.params) for r in hdr.records] != [_shape(r.params) for r in new_hdr.records] or \
            [r.keyword for r in hdr.records] != [r.keyword for r in new_hdr.records]:
        return "header_structure_changed"
    if len(hdr.strings) != len(new_hdr.strings):
        return "header_string_count_changed"
    edited = {e.start: e for e in plan.edits}
    old_text, new_text = hdr.text, new_hdr.text
    old_gap = new_gap = 0
    for o, n in zip(hdr.strings, new_hdr.strings):
        if old_text[old_gap:o.start] != new_text[new_gap:n.start]:
            return "header_non_string_bytes_changed"
        e = edited.get(o.start)
        raw_new = new_text[n.start:n.end]
        if e is None:
            if old_text[o.start:o.end] != raw_new:
                return "unplanned_header_string_changed"
        else:
            if n.field != e.field or raw_new != e.new_raw:
                return "replacement_not_as_planned"
            if _split_content(raw_new[1:-1])[0] != e.expected_text:
                return "replacement_roundtrip_mismatch"
            if "'" + _escape(e.expected_text) + "'" != raw_new:
                return "replacement_not_canonical"
        old_gap, new_gap = o.end, n.end
    if old_text[old_gap:] != new_text[new_gap:]:
        return "header_non_string_bytes_changed"
    # mapped originals must be gone from transformed fields (unless a replacement contains it by design)
    reintroduced = {old for old in idmap.strings if any(old in nv for nv in idmap.strings.values())}
    for old in idmap.strings:
        if old in reintroduced:
            continue
        for e in plan.edits:
            if old in e.expected_text:
                return "mapped_original_present_after_replace"
    if not plan.edits:
        return "no_replacement_occurred"
    return ""


def residual_counts(path: str | Path, idmap: IdentityMap) -> dict[int, int]:
    """Raw byte occurrences of each mapped identity anywhere in the file (index -> count). Streaming."""
    needles: list[tuple[int, bytes]] = []
    for i, old in enumerate(idmap.strings):
        for enc in {"utf-8", "cp1252"}:
            try:
                needles.append((i, old.encode(enc)))
            except UnicodeEncodeError:
                pass
    needles = [(i, nb) for i, nb in dict.fromkeys(needles)]
    out: dict[int, int] = {}
    if not needles:
        return out
    keep = max(len(nb) for _, nb in needles) - 1
    carry = b""
    with Path(path).open("rb") as f:
        while chunk := f.read(CHUNK):
            buf = carry + chunk
            for i, nb in needles:
                # count only occurrences ending inside the new chunk to avoid double counting carry
                start = 0
                while (j := buf.find(nb, start)) >= 0:
                    if j + len(nb) > len(carry):
                        out[i] = out.get(i, 0) + 1
                    start = j + 1
            carry = buf[-keep:] if keep else b""
    return dict(sorted(out.items()))
