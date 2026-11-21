"""Synthetic CFB v3 writer + SummaryInformation builder for OLE tests (no real data)."""
from __future__ import annotations

import struct

from machina_deid.ole_transform import FMTID_SUMMARY, MAGIC

SEC, MINI = 512, 64
END, FREE, FATS = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD


def summary_info(props: dict[int, tuple[int, object]], codepage: int | None = 1252) -> bytes:
    """props: pid -> (vt, value). Supports VT_LPSTR(0x1E, str), VT_LPWSTR(0x1F, str), VT_I4(3, int)."""
    items = []
    if codepage is not None:
        items.append((1, struct.pack("<Ih2x", 2, codepage)))
    for pid, (vt, val) in props.items():
        if vt == 0x1E:
            raw = val.encode(f"cp{codepage}") + b"\0"
            body = struct.pack("<II", vt, len(raw)) + raw
        elif vt == 0x1F:
            raw = val.encode("utf-16-le") + b"\0\0"
            body = struct.pack("<II", vt, len(raw) // 2) + raw
        else:
            body = struct.pack("<Ii", vt, val)
        items.append((pid, body + b"\0" * (-len(body) % 4)))
    table_len = 8 + 8 * len(items)
    offs, pos = [], table_len
    for _, b in items:
        offs.append(pos)
        pos += len(b)
    section = struct.pack("<II", pos, len(items)) + b"".join(
        struct.pack("<II", pid, o) for (pid, _), o in zip(items, offs)) + b"".join(b for _, b in items)
    head = b"\xFE\xFF\x00\x00" + struct.pack("<I", 0x00020006) + b"\0" * 16 + struct.pack("<I", 1)
    return head + FMTID_SUMMARY + struct.pack("<I", 48) + section


def cfb(streams: dict[str, bytes]) -> bytes:
    """Flat CFB v3 (all streams under root). Streams < 4096 bytes go to the mini stream."""
    names = sorted(streams, key=lambda n: (len(n), n.upper()))
    sectors: list[bytes] = []
    fat: dict[int, int] = {}

    def put(blob: bytes) -> int:
        if not blob:
            return END
        first = len(sectors)
        chunks = [blob[i:i + SEC].ljust(SEC, b"\0") for i in range(0, len(blob), SEC)]
        for k, c in enumerate(chunks):
            fat[first + k] = first + k + 1 if k + 1 < len(chunks) else END
            sectors.append(c)
        return first

    mini_blob, minifat, starts = b"", [], {}
    for n in names:
        d = streams[n]
        if 0 < len(d) < 4096:
            first = len(mini_blob) // MINI
            cnt = -(-len(d) // MINI)
            minifat.extend(first + k + 1 if k + 1 < cnt else END for k in range(cnt))
            mini_blob += d.ljust(cnt * MINI, b"\0")
            starts[n] = first
    for n in names:
        if len(streams[n]) >= 4096:
            starts[n] = put(streams[n])
        elif not streams[n]:
            starts[n] = END
    root_start = put(mini_blob)
    mf_raw = b"".join(struct.pack("<I", x) for x in minifat)
    mf_start = put(mf_raw) if mf_raw else END
    n_mf = -(-len(mf_raw) // SEC)

    def ent(name, kind, start, size, child=0xFFFFFFFF, right=0xFFFFFFFF):
        nm = name.encode("utf-16-le") + b"\0\0"
        return (nm.ljust(64, b"\0") + struct.pack("<HBB", len(nm), kind, 1) +
                struct.pack("<III", 0xFFFFFFFF, right, child) + b"\0" * 16 + b"\0" * 4 + b"\0" * 16 +
                struct.pack("<III", start, size, 0))

    dirs = [ent("Root Entry", 5, root_start, len(mini_blob), child=1 if names else 0xFFFFFFFF)]
    for i, n in enumerate(names):
        dirs.append(ent(n, 2, starts[n], len(streams[n]), right=i + 2 if i + 1 < len(names) else 0xFFFFFFFF))
    raw_dir = b"".join(dirs)
    raw_dir += b"\0" * (-len(raw_dir) % SEC)
    dir_start = put(raw_dir)
    n_fat = 1
    while n_fat * 128 < len(sectors) + n_fat:
        n_fat += 1
    fat_sids = list(range(len(sectors), len(sectors) + n_fat))
    table = [fat.get(i, FREE) for i in range(len(sectors))] + [FATS] * n_fat
    table += [FREE] * (n_fat * 128 - len(table))
    fat_raw = b"".join(struct.pack("<I", x) for x in table)
    sectors.extend(fat_raw[i:i + SEC] for i in range(0, len(fat_raw), SEC))
    hdr = MAGIC + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
    hdr += struct.pack("<IIII", 0, n_fat, dir_start, 0) + struct.pack("<IIIII", 4096, mf_start, n_mf, END, 0)
    difat = fat_sids + [FREE] * (109 - len(fat_sids))
    hdr += b"".join(struct.pack("<I", x) for x in difat)
    assert len(hdr) == SEC
    return hdr + b"".join(sectors)


def cstring(s: str) -> bytes:
    n = len(s)
    if n < 0xFF:
        p = b"\xFF\xFE\xFF" + bytes([n])
    else:
        p = b"\xFF\xFE\xFF\xFF" + struct.pack("<H", n)
    return p + s.encode("utf-16-le")
