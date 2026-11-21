"""Standalone OLE / CFB (Compound File Binary) account pseudonymization core.

Scope (evidence-driven, see docs/ole-core.md):

* Parsed source of truth: ``\\x05SummaryInformation`` property set, PIDSI_LASTAUTHOR (pid 8) by default,
  typed VT_LPSTR (codepage-aware) or VT_LPWSTR.
* Cross-stream *verified* record kinds (writable):
    - ``cstring``: MFC Unicode CString ``FF FE FF <u8 len>`` (or the ``FF FF <u16>`` / ``FF FF FF FF <u32>``
      long forms) whose declared character length equals the whole value and whose content equals it exactly.
    - ``path_component``: a UTF-16LE match that is a whole token of a printable UTF-16LE run that contains a path
      separator; both neighbours must be a separator (``\\ / @ .``), a NUL/non-printable run boundary, or ``:``.
* Everything else (bare UTF-16LE substrings, ASCII/ANSI occurrences) is *informative only*: counted, never written.

Writes are equal-length, in place, inside the stream's own sectors.  The CFB header, FAT/MiniFAT/DIFAT,
directory, stream sizes, unparsed bytes, compressed payloads and preview streams are never touched; every
write is to a span inside a parsed record.  The caller supplies the deterministic alias (``equal_length_alias``
is a helper); unequal-length aliases are rejected rather than padded.

``inspect_result`` is an independent check: it re-parses the output from bytes, compares every stream with the
input outside the recorded spans and confirms no verified-kind residue of a mapped original remains.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass, field

MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF
MAX_BYTES = 2 * 1024 ** 3
MAX_ENTRIES = 200_000
SUMMARY = "\x05SummaryInformation"
FMTID_SUMMARY = bytes.fromhex("e0859ff2f94f6810ab9108002b27b3d9")  # F29F85E0-4FF9-1068-AB91-08002B27B3D9
PID_CODEPAGE, PID_AUTHOR, PID_LASTAUTHOR = 1, 4, 8
VT_I2, VT_LPSTR, VT_LPWSTR = 0x02, 0x1E, 0x1F
SEPS = frozenset("\\/@.")
SKIP_STREAM_HINTS = ("preview", "thumbnail")  # never written; only reported
VERIFIED_KINDS = ("property", "cstring", "path_component")


class OleError(Exception):
    """Reason code only (never contains document content)."""


# --------------------------------------------------------------------------------------------- CFB parsing

@dataclass
class Entry:
    sid: int
    name: str
    path: str
    kind: int  # 1 storage, 2 stream, 5 root
    start: int
    size: int


@dataclass
class Cfb:
    data: bytes
    sector: int
    mini: int
    cutoff: int
    fat: list[int]
    minifat: list[int]
    entries: list[Entry]
    root_chunks: list[tuple[int, int]]

    def streams(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == 2]

    def chunks(self, e: Entry) -> list[tuple[int, int]]:
        """(file offset, length) runs covering exactly the stream's ``size`` bytes."""
        if e.size == 0:
            return []
        out: list[tuple[int, int]] = []
        left = e.size
        if e.size < self.cutoff:
            for ms in _chain(self.minifat, e.start, -(-e.size // self.mini)):
                off = ms * self.mini
                n = min(self.mini, left)
                out.extend(_slice(self.root_chunks, off, n))
                left -= n
        else:
            for s in _chain(self.fat, e.start, -(-e.size // self.sector)):
                n = min(self.sector, left)
                fo = (s + 1) * self.sector
                if fo + n > len(self.data):
                    raise OleError("sector_out_of_range")
                out.append((fo, n))
                left -= n
        if left:
            raise OleError("short_chain")
        return _merge(out)

    def read(self, e: Entry) -> bytes:
        return b"".join(self.data[o:o + n] for o, n in self.chunks(e))


def _merge(chunks):
    out = []
    for o, n in chunks:
        if out and out[-1][0] + out[-1][1] == o:
            out[-1] = (out[-1][0], out[-1][1] + n)
        else:
            out.append((o, n))
    return out


def _slice(chunks, off, n):
    out, pos = [], 0
    for o, ln in chunks:
        if n <= 0:
            break
        if off < pos + ln:
            a = max(off - pos, 0)
            take = min(ln - a, n)
            out.append((o + a, take))
            off += take
            n -= take
        pos += ln
    if n:
        raise OleError("mini_out_of_range")
    return out


def _chain(table: list[int], start: int, need: int | None = None) -> list[int]:
    out, seen, s = [], set(), start
    while s not in (ENDOFCHAIN, FREESECT):
        if s >= len(table) or s in seen or len(out) > len(table):
            raise OleError("bad_chain")
        seen.add(s)
        out.append(s)
        if need is not None and len(out) == need:
            return out
        s = table[s]
    if need is not None and len(out) < need:
        raise OleError("short_chain")
    return out


def is_cfb(data: bytes) -> bool:
    return data[:8] == MAGIC


def parse(data: bytes) -> Cfb:
    if not is_cfb(data) or len(data) < 512:
        raise OleError("not_cfb")
    if len(data) > MAX_BYTES:
        raise OleError("too_large")
    shift, mshift = struct.unpack_from("<HH", data, 0x1E)
    if shift not in (9, 12) or mshift != 6:
        raise OleError("bad_sector_shift")
    sec, mini = 1 << shift, 1 << mshift
    n_fat, dir_start = struct.unpack_from("<II", data, 0x2C)
    cutoff, mf_start, n_mf, dif_start, n_dif = struct.unpack_from("<IIIII", data, 0x38)
    if cutoff != 4096:
        raise OleError("bad_cutoff")
    nsec = (len(data) + sec - 1) // sec - 1

    def sector(s):
        if s >= nsec:
            raise OleError("sector_out_of_range")
        return data[(s + 1) * sec:(s + 2) * sec]

    difat = [x for x in struct.unpack_from("<109I", data, 0x4C)]
    s, hops = dif_start, 0
    while s not in (ENDOFCHAIN, FREESECT) and hops < max(n_dif, 1) + 1:
        blk = struct.unpack(f"<{sec // 4}I", sector(s).ljust(sec, b"\0"))
        difat.extend(blk[:-1])
        s, hops = blk[-1], hops + 1
    fat_sids = [x for x in difat if x not in (FREESECT, ENDOFCHAIN)][:n_fat]
    if len(fat_sids) != n_fat:
        raise OleError("bad_difat")
    fat: list[int] = []
    for fs in fat_sids:
        fat.extend(struct.unpack(f"<{sec // 4}I", sector(fs).ljust(sec, b"\0")))
    minifat: list[int] = []
    if n_mf and mf_start != ENDOFCHAIN:
        for ms in _chain(fat, mf_start):
            minifat.extend(struct.unpack(f"<{sec // 4}I", sector(ms).ljust(sec, b"\0")))
    raw_dir = b"".join(sector(d) for d in _chain(fat, dir_start))
    count = len(raw_dir) // 128
    if count > MAX_ENTRIES:
        raise OleError("too_many_entries")
    ents: dict[int, tuple] = {}
    for i in range(count):
        r = raw_dir[i * 128:(i + 1) * 128]
        nlen = struct.unpack_from("<H", r, 64)[0]
        kind = r[66]
        if kind == 0:
            continue
        if nlen > 64 or nlen % 2:
            raise OleError("bad_name")
        name = r[:max(nlen - 2, 0)].decode("utf-16-le", "strict")
        left, right, child = struct.unpack_from("<III", r, 68)
        start, size_lo, size_hi = struct.unpack_from("<III", r, 116)
        size = size_lo | ((size_hi << 32) if shift == 12 else 0)
        ents[i] = (name, kind, left, right, child, start, size)
    if 0 not in ents or ents[0][1] != 5:
        raise OleError("no_root")
    entries: list[Entry] = []
    seen: set[int] = set()

    def walk(sid, prefix, depth):
        stack = [sid]
        while stack:
            s = stack.pop()
            if s == NOSTREAM:
                continue
            if s not in ents or s in seen or depth > 64:
                raise OleError("bad_tree")
            seen.add(s)
            name, kind, left, right, child, start, size = ents[s]
            path = f"{prefix}/{name}" if prefix else name
            entries.append(Entry(s, name, path, kind, start, size))
            stack.extend((left, right))
            if kind == 1:
                walk(child, path, depth + 1)

    r = ents[0]
    entries.append(Entry(0, r[0], "", 5, r[5], r[6]))
    seen.add(0)
    walk(r[4], "", 1)
    root = entries[0]
    root_chunks: list[tuple[int, int]] = []
    if root.size:
        left = root.size
        for s in _chain(fat, root.start, -(-root.size // sec)):
            n = min(sec, left)
            root_chunks.append(((s + 1) * sec, n))
            left -= n
    entries.sort(key=lambda e: e.path)
    return Cfb(data, sec, mini, cutoff, fat, minifat, entries, _merge(root_chunks))


# ------------------------------------------------------------------------------------ property set parsing

@dataclass
class Prop:
    stream: str
    pid: int
    vt: int
    value: str
    encoding: str
    value_off: int   # offset within stream of the value bytes (excluding terminator)
    value_len: int   # byte length of value (excluding terminator / padding)


def _codec(cp: int | None) -> str:
    if cp is None:
        return "cp1252"
    if cp in (1200,):
        return "utf-16-le"
    if cp == 65001:
        return "utf-8"
    if cp < 0:
        cp &= 0xFFFF
    return f"cp{cp}"


def parse_properties(blob: bytes, stream: str = SUMMARY, pids=(PID_LASTAUTHOR,)) -> list[Prop]:
    """Exact typed parse of selected string properties in the FMTID_SummaryInformation section."""
    if len(blob) < 48 or blob[:2] != b"\xFE\xFF":
        raise OleError("bad_propset")
    nsec = struct.unpack_from("<I", blob, 24)[0]
    if nsec < 1 or nsec > 8 or 28 + 20 * nsec > len(blob):
        raise OleError("bad_propset")
    out: list[Prop] = []
    for k in range(nsec):
        fmtid = blob[28 + 20 * k:44 + 20 * k]
        soff = struct.unpack_from("<I", blob, 44 + 20 * k)[0]
        if fmtid != FMTID_SUMMARY:
            continue
        if soff + 8 > len(blob):
            raise OleError("bad_section")
        ssize, nprop = struct.unpack_from("<II", blob, soff)
        end = soff + ssize
        if end > len(blob) or nprop > 4096 or 8 + 8 * nprop > ssize:
            raise OleError("bad_section")
        table = [struct.unpack_from("<II", blob, soff + 8 + 8 * i) for i in range(nprop)]
        cp = None
        for pid, po in table:
            if pid == PID_CODEPAGE and soff + po + 6 <= end:
                vt = struct.unpack_from("<I", blob, soff + po)[0] & 0xFFFF
                if vt == VT_I2:
                    cp = struct.unpack_from("<h", blob, soff + po + 4)[0]
        for pid, po in table:
            if pid not in pids:
                continue
            a = soff + po
            if a + 8 > end:
                raise OleError("bad_property")
            vt_full, cnt = struct.unpack_from("<II", blob, a)
            vt = vt_full & 0xFFFF
            if vt_full >> 16 or vt not in (VT_LPSTR, VT_LPWSTR):
                continue  # not a scoped string type: leave alone
            body = a + 8
            if vt == VT_LPWSTR:
                nb = cnt * 2
                if body + nb > end:
                    raise OleError("bad_property")
                raw = blob[body:body + nb]
                cut = next((i for i in range(0, nb, 2) if raw[i:i + 2] == b"\0\0"), nb)
                enc = "utf-16-le"
            else:
                if body + cnt > end:
                    raise OleError("bad_property")
                raw = blob[body:body + cnt]
                enc = _codec(cp)
                if enc == "utf-16-le":
                    cut = next((i for i in range(0, cnt - 1, 2) if raw[i:i + 2] == b"\0\0"), cnt)
                else:
                    cut = raw.find(b"\0")
                    cut = cnt if cut < 0 else cut
            try:
                val = raw[:cut].decode(enc, "strict")
            except (UnicodeDecodeError, LookupError):
                raise OleError("undecodable_property")
            out.append(Prop(stream, pid, vt, val, enc, body, cut))
    return out


def summary_props(cfb: Cfb, pids=(PID_LASTAUTHOR,)) -> list[Prop]:
    out = []
    for e in cfb.streams():
        if e.path == SUMMARY:
            out.extend(parse_properties(cfb.read(e), e.path, pids))
    return out


# ------------------------------------------------------------------------------- cross-stream occurrences

@dataclass(frozen=True)
class Hit:
    stream: str
    kind: str     # property | cstring | path_component | raw_utf16 | raw_ascii
    off: int      # offset of the value bytes within the stream
    length: int   # byte length of the value
    original: str
    encoding: str

    @property
    def verified(self) -> bool:
        return self.kind in VERIFIED_KINDS


def _printable16(blob: bytes, i: int) -> bool:
    if i < 0 or i + 2 > len(blob):
        return False
    c = struct.unpack_from("<H", blob, i)[0]
    return 0x20 <= c and c != 0x7F and not 0xD800 <= c <= 0xDFFF


def _cstring_ok(blob: bytes, off: int, nchars: int) -> bool:
    """MFC CArchive Unicode CString prefix immediately before ``off`` declaring exactly ``nchars``."""
    if nchars < 0xFF:
        return off >= 4 and blob[off - 4:off - 1] == b"\xFF\xFE\xFF" and blob[off - 1] == nchars
    if nchars < 0xFFFE:
        return off >= 6 and blob[off - 6:off - 2] == b"\xFF\xFE\xFF\xFF" and \
            struct.unpack_from("<H", blob, off - 2)[0] == nchars
    return off >= 10 and blob[off - 10:off - 4] == b"\xFF\xFE\xFF\xFF\xFF\xFF" and \
        struct.unpack_from("<I", blob, off - 4)[0] == nchars


def _path_component_ok(blob: bytes, off: int, nbytes: int) -> bool:
    """Whole token of a printable UTF-16LE run that contains a path/contact separator."""
    def ch(i):
        return chr(struct.unpack_from("<H", blob, i)[0]) if _printable16(blob, i) else None
    before, after = ch(off - 2), ch(off + nbytes)
    ok_b = before is None or before in SEPS or before == ":"
    ok_a = after is None or after in SEPS
    if not (ok_b and ok_a) or (before is None and after is None):
        return False
    # the enclosing run must contain a separator adjacent to the token (already true) and be bounded
    a = off
    while a - 2 >= 0 and _printable16(blob, a - 2) and off - a < 4096:
        a -= 2
    b = off + nbytes
    while _printable16(blob, b) and b - off < 4096:
        b += 2
    run = blob[a:b].decode("utf-16-le", "replace")
    return any(c in SEPS for c in run)


def _find_all(blob: bytes, needle: bytes):
    i = blob.find(needle)
    while i >= 0:
        yield i
        i = blob.find(needle, i + 1)


def scan(cfb: Cfb, values: list[str], props: list[Prop] | None = None) -> list[Hit]:
    """Classify every occurrence of each value. Only ``Hit.verified`` hits are writable."""
    props = summary_props(cfb) if props is None else props
    hits: list[Hit] = [Hit(p.stream, "property", p.value_off, p.value_len, p.value, p.encoding)
                       for p in props if p.value in values]
    taken = {(h.stream, h.off) for h in hits}
    for e in cfb.streams():
        blob = cfb.read(e)
        for v in values:
            if not v:
                continue
            u = v.encode("utf-16-le")
            for i in _find_all(blob, u):
                if (e.path, i) in taken:
                    continue
                if _cstring_ok(blob, i, len(v)):
                    kind = "cstring"
                elif _path_component_ok(blob, i, len(u)):
                    kind = "path_component"
                else:
                    kind = "raw_utf16"
                if kind != "raw_utf16" and any(h in e.path.lower() for h in SKIP_STREAM_HINTS):
                    kind = "raw_utf16"
                hits.append(Hit(e.path, kind, i, len(u), v, "utf-16-le"))
                taken.add((e.path, i))
            try:
                a = v.encode("cp1252")
            except UnicodeEncodeError:
                a = None
            if a and a != u:
                for i in _find_all(blob, a):
                    if (e.path, i) not in taken:
                        hits.append(Hit(e.path, "raw_ascii", i, len(a), v, "cp1252"))
                        taken.add((e.path, i))
    return hits


# ------------------------------------------------------------------------------------------------ aliasing

_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"


def equal_length_alias(original: str, key: bytes) -> str:
    """Deterministic keyed alias of exactly ``len(original)`` ASCII characters (works for 1..n chars).

    Starts with a letter; ASCII so every record encoding (cp*, utf-8, utf-16) keeps the byte length when the
    original is also single-byte. Callers may supply their own alias instead.
    """
    if not original:
        raise OleError("empty_original")
    out, ctr = "", 0
    while len(out) < len(original):
        d = hmac.new(key, f"{ctr}:{original}".encode("utf-8"), hashlib.sha256).digest()
        out += "".join(_ALNUM[b % 36] for b in d)
        ctr += 1
    out = _ALNUM[int.from_bytes(hmac.new(key, original.encode(), hashlib.sha256).digest()[:2], "big") % 26] + out[1:]
    out = out[:len(original)]
    if out == original:
        out = ("z" if original[0] != "z" else "y") + out[1:]
    return out


# ----------------------------------------------------------------------------------------------- transform

@dataclass
class Write:
    stream: str
    kind: str
    off: int
    length: int
    file_spans: list[tuple[int, int]]


@dataclass
class Result:
    data: bytes
    writes: list[Write] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    reason: str | None = None


def _encode(alias: str, enc: str) -> bytes:
    return alias.encode(enc, "strict")


def transform(data: bytes, mapping: dict[str, str], pids=(PID_LASTAUTHOR,)) -> Result:
    """Apply caller aliases to verified parsed records only. Returns original bytes on any refusal."""
    try:
        cfb = parse(data)
        props = summary_props(cfb, pids)
    except OleError as exc:
        return Result(data, reason=str(exc))
    for o, a in mapping.items():
        if not o or len(o) != len(a) or o == a:
            return Result(data, reason="alias_length_mismatch")
    # Only values confirmed by a parsed property are candidates; mapping keys outside are ignored.
    values = sorted({p.value for p in props if p.value in mapping}, key=lambda v: (-len(v), v))
    hits = scan(cfb, values, props)
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.kind] = counts.get(h.kind, 0) + 1
    if not values:
        return Result(data, counts=counts, reason="no_mapped_property")
    buf = bytearray(data)
    by_stream = {e.path: e for e in cfb.streams()}
    writes: list[Write] = []
    claimed: list[tuple[int, int]] = []
    for h in sorted(hits, key=lambda h: (h.stream, h.off)):
        if not h.verified:
            continue
        try:
            new = _encode(mapping[h.original], h.encoding)
        except UnicodeEncodeError:
            return Result(data, counts=counts, reason="alias_unencodable")
        if len(new) != h.length:
            return Result(data, counts=counts, reason="alias_length_mismatch")
        spans = _slice(cfb.chunks(by_stream[h.stream]), h.off, h.length)
        for fo, n in spans:
            if any(fo < b and a < fo + n for a, b in claimed):
                return Result(data, counts=counts, reason="overlapping_write")
        pos = 0
        for fo, n in spans:
            buf[fo:fo + n] = new[pos:pos + n]
            claimed.append((fo, fo + n))
            pos += n
        writes.append(Write(h.stream, h.kind, h.off, h.length, spans))
    out = bytes(buf)
    rep = inspect_result(data, out, mapping, writes, pids)
    if not rep["ok"]:
        return Result(data, counts=counts, reason="verify_failed:" + rep["reason"])
    return Result(out, writes, counts)


# --------------------------------------------------------------------------------- independent inspection

def inspect_result(before: bytes, after: bytes, mapping: dict[str, str], writes: list[Write],
                   pids=(PID_LASTAUTHOR,)) -> dict:
    """Re-parse both files from bytes and check structural identity + verified-residue absence."""
    def fail(r):
        return {"ok": False, "reason": r}
    if len(before) != len(after):
        return fail("length_changed")
    try:
        a, b = parse(before), parse(after)
    except OleError as exc:
        return fail(f"reparse:{exc}")
    sig = lambda c: [(e.path, e.kind, e.start, e.size) for e in c.entries]  # noqa: E731
    if sig(a) != sig(b) or a.fat != b.fat or a.minifat != b.minifat:
        return fail("structure_changed")
    allowed = bytearray(len(before))
    for w in writes:
        for fo, n in w.file_spans:
            allowed[fo:fo + n] = b"\1" * n
    for i in range(len(before)):
        if before[i] != after[i] and not allowed[i]:
            return fail("unrecorded_change")
    sa = {e.path: e for e in a.streams()}
    for w in writes:
        old, new = a.read(sa[w.stream])[w.off:w.off + w.length], b.read(sa[w.stream])[w.off:w.off + w.length]
        if old == new:
            return fail("noop_write")
    values = sorted({p.value for p in summary_props(a, pids) if p.value in mapping})
    after_hits = scan(b, values, summary_props(b, pids))
    if any(h.verified for h in after_hits):
        return fail("verified_residue")
    raw = len(after_hits)
    return {"ok": True, "reason": None, "writes": len(writes), "informative_raw_remaining": raw}


def inspect(data: bytes) -> dict:
    """Redacted inspection: counts, lengths and kinds only — never values."""
    cfb = parse(data)
    props = summary_props(cfb)
    vals = sorted({p.value for p in props if p.value})
    hits = scan(cfb, vals, props)
    kinds: dict[str, int] = {}
    for h in hits:
        kinds[h.kind] = kinds.get(h.kind, 0) + 1
    return {
        "streams": len(cfb.streams()),
        "storages": sum(1 for e in cfb.entries if e.kind == 1),
        "sector_size": cfb.sector,
        "solidworks_hint": any("swx" in e.name.lower() or "contents" == e.name.lower() for e in cfb.entries),
        "lastauthor": [{"len": len(p.value), "vt": "LPWSTR" if p.vt == VT_LPWSTR else "LPSTR",
                        "encoding": p.encoding} for p in props],
        "occurrences": kinds,
        "verified": sum(v for k, v in kinds.items() if k in VERIFIED_KINDS),
        "informative": sum(v for k, v in kinds.items() if k not in VERIFIED_KINDS),
    }
