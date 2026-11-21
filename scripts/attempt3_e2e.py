"""Attempt 3: labeled synthetic end-to-end harness (PDF identity removal / pairs / triplets, OLE accounts).

Ground truth (value, identity, page, rect) is written by the generator, never by the pipeline. The pipeline
under test is the real production path: ``scope.build_scope`` (pass 1.5, frozen map) -> ``twopass.transform_file``
(pass 2). A file that is not transformed is scored from the *source* bytes (baseline passthrough), so misses
and rejections count against the denominator.

Independent readers: occurrence text is read back from the output with ``page.get_textbox(rect)`` plus whole
document plain text / metadata / annotation scan; OLE via ``olefile``-free raw stream reads of our own CFB
helper (property parse in ole_transform is NOT used for scoring - byte search over whole file).

Usage:  python scripts/attempt3_e2e.py [--json out.json]
Prints value-free aggregate counts only.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import sys
import tempfile
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from machina_deid import scope as sc  # noqa: E402
from machina_deid import twopass  # noqa: E402
from ole_helpers import cfb, summary_info  # noqa: E402

# Synthetic identities (invented).
ID = {
    "P1": ["Jonathan Whitfield", "JONATHAN WHITFIELD"],
    "P2": ["Maria Keller"],
    "P3": ["Tomas Lindqvist"],
    "P4": ["Ana Duarte"],
    "P5": ["Rui Okafor"],
    "O1": ["Brightline Fabrication GmbH"],
    "E1": ["m.keller@brightline-fab.example"],
}


class Doc:
    def __init__(self, rel):
        self.rel = rel
        self.doc = pymupdf.open()
        self.occ = []  # (identity, value, page, rect)

    def page(self, w=842, h=595, rot=0):
        p = self.doc.new_page(width=w, height=h)
        if rot:
            p.set_rotation(rot)
        return p

    def text(self, p, x, y, s, size=8, ident=None, font="helv"):
        p.insert_text((x, y), s, fontsize=size, fontname=font)
        if ident:
            wd = pymupdf.get_text_length(s, fontname=font, fontsize=size)
            self.occ.append((ident, s, p.number, (x - 0.5, y - size * 0.95, x + wd + 0.5, y + size * 0.3)))

    def grid(self, p, x0, y0, cols, rows, rh=12):
        xs = [x0]
        for c in cols:
            xs.append(xs[-1] + c)
        for r in range(rows + 1):
            p.draw_line((x0, y0 + r * rh), (xs[-1], y0 + r * rh), width=0.5)
        for x in xs:
            p.draw_line((x, y0), (x, y0 + rows * rh), width=0.5)

    def titleblock(self, p, rows, x0=560, y0=480, cols=(60, 110, 50)):
        """rows: [(label, (ident, value) | None, date)]"""
        self.grid(p, x0, y0, cols, len(rows))
        for i, (label, who, date) in enumerate(rows):
            y = y0 + i * 12 + 9
            self.text(p, x0 + 3, y, label, 7)
            if who:
                self.text(p, x0 + cols[0] + 3, y, who[1], 7, who[0])
            if date:
                self.text(p, x0 + cols[0] + cols[1] + 3, y, date, 7)

    def save(self, base: Path, author=None):
        if author:
            self.doc.set_metadata({"author": author[1]})
            self.occ.append((author[0], author[1], -1, None))
        out = base / self.rel
        out.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(out)
        self.doc.close()
        return out


def engineering_noise(d: Doc, p):
    for i, s in enumerate(("MATERIAL: STAINLESS STEEL 304", "FINISH: ANODIZE", "BRACKET ASSY", "SCALE 1:2",
                           "DWG NO. 10-2231-A", "UNLESS OTHERWISE SPECIFIED DIMENSIONS ARE IN MM")):
        d.text(p, 40, 40 + 12 * i, s, 8)
    p.draw_rect((100, 150, 400, 400), width=1)
    p.draw_circle((250, 275), 60, width=1)


def make_corpus(base: Path) -> tuple[list[Path], list[dict]]:
    docs = []
    # famA: classic title block, label-right
    d = Doc("famA/drw-100.pdf")
    p = d.page()
    engineering_noise(d, p)
    d.titleblock(p, [("DRAWN", ("P1", "Jonathan Whitfield"), "2021-03-02"), ("CHECKED", ("P2", "Maria Keller"), "2021-03-04"),
                     ("APPROVED", ("P3", "Tomas Lindqvist"), "2021-03-09"), ("COMPANY", ("O1", "Brightline Fabrication GmbH"), None)])
    docs.append(d.save(base, author=("P1", "Jonathan Whitfield")))
    # famA: same people swapped roles, plus a narrow cell that forces small extents
    d = Doc("famA/drw-101.pdf")
    p = d.page()
    engineering_noise(d, p)
    d.titleblock(p, [("DRAWN", ("P2", "Maria Keller"), "2021-04-01"), ("CHECKED", ("P1", "Jonathan Whitfield"), "2021-04-02"),
                     ("APPROVED", ("P4", "Ana Duarte"), None)], cols=(40, 62, 40))
    docs.append(d.save(base))
    # famB: inline labels without colon / with trailing date; contact email
    d = Doc("famB/spec-200.pdf")
    p = d.page(595, 842)
    d.text(p, 40, 60, "DRAWN", 8)
    d.text(p, 78, 60, "Ana Duarte", 8, "P4")
    d.text(p, 40, 74, "CHECKED BY", 8)
    d.text(p, 98, 74, "Rui Okafor", 8, "P5")
    d.text(p, 40, 88, "Questions to: ", 8)
    d.text(p, 98, 88, "m.keller@brightline-fab.example", 8, "E1")
    d.text(p, 40, 120, "Prepared by Tomas Lindqvist", 8)
    d.occ.append(("P3", "Tomas Lindqvist", 0, (40 + pymupdf.get_text_length("Prepared by ", fontsize=8) - 0.5, 120 - 7.6,
                                                40 + pymupdf.get_text_length("Prepared by Tomas Lindqvist", fontsize=8) + 0.5, 122.4)))
    d.text(p, 40, 150, "MATERIAL: ALUMINIUM 6061-T6", 8)
    docs.append(d.save(base))
    # famC: rotated sheet, title block in upper-case
    d = Doc("famC/drw-300.pdf")
    p = d.page(rot=90)
    engineering_noise(d, p)
    d.titleblock(p, [("DRN", ("P1", "JONATHAN WHITFIELD"), "02.03.21"), ("CHK", ("P5", "Rui Okafor"), "03.03.21")])
    docs.append(d.save(base))
    # famC: two pages, second page unlabeled mention of an identity labeled elsewhere
    d = Doc("famC/drw-301.pdf")
    p = d.page()
    engineering_noise(d, p)
    d.titleblock(p, [("GEZEICHNET", ("P3", "Tomas Lindqvist"), "01.02.2022"), ("GEPRÜFT", ("P2", "Maria Keller"), None)])
    p2 = d.page()
    d.text(p2, 40, 60, "Revision notes: updated per Maria Keller markup", 8)
    d.occ.append(("P2", "Maria Keller", 1, (40 + pymupdf.get_text_length("Revision notes: updated per ", fontsize=8) - 0.5,
                                           60 - 7.6, 40 + pymupdf.get_text_length("Revision notes: updated per Maria Keller",
                                                                                  fontsize=8) + 0.5, 62.4)))
    docs.append(d.save(base))
    # control: no identities, confusable technical text
    d = Doc("famD/ctl-400.pdf")
    p = d.page()
    engineering_noise(d, p)
    d.titleblock(p, [("DRAWN", None, None), ("CHECKED", None, None), ("MATERIAL", None, None)])
    docs.append(d.save(base))
    labels = []
    for dpath in docs:
        pass
    return docs


def ole_corpus(base: Path):
    """CFB files with Author + LastAuthor and cross-stream records (MFC CString, NUL-terminated UTF-16)."""
    out = []
    specs = [("famE/part-1.sldprt", "jwhitfield", "mkeller"), ("famE/part-2.sldprt", "mkeller", "mkeller"),
             ("famE/asm-3.sldasm", "tlindqvist", "jwhitfield")]
    for rel, author, last in specs:
        si = summary_info({4: (0x1E, author), 8: (0x1E, last), 2: (0x1E, "BRACKET")})
        u = last.encode("utf-16-le")
        contents = (b"\x00" * 40 + b"\xFF\xFE\xFF" + bytes([len(last)]) + u + b"\x00" * 16
                    + struct.pack("<I", len(author)) + author.encode("utf-16-le") + b"\x00\x00" + b"\x01" * 64)
        data = cfb({"\x05SummaryInformation": si, "Contents": contents})
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        out.append((p, rel, {author, last}))
    return out


class _NoCatalog:
    def approved_pseudonym(self, v):
        return None


def _norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


_PSEUDO = re.compile(r"[a-z]+-[0-9a-f]{4,}(?:@[\w.-]+)?", re.I)


def _replacement_token(got: str) -> str:
    """Pseudonym-shaped token read back in the occurrence box (independent of pipeline internals)."""
    m = _PSEUDO.search(got or "")
    return m.group(0).lower() if m else ""


def _doc_text(path: Path) -> str:
    d = pymupdf.open(path)
    parts = [str(v) for v in (d.metadata or {}).values() if v]
    for x in range(1, d.xref_length()):
        try:
            if d.xref_is_stream(x) and d.xref_get_key(x, "Type")[1] == "/Metadata":
                parts.append((d.xref_stream(x) or b"").decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            pass
    for pg in d:
        parts.append(pg.get_text("text"))
        for a in pg.annots() or []:
            parts += [a.info.get("content", ""), a.info.get("title", "")]
        for ln in pg.get_links():
            parts.append(ln.get("uri") or "")
    d.close()
    return _norm(" ".join(parts))


def run(work: Path) -> dict:
    inp = work / "in"
    pdfs = make_corpus(inp)
    oles = ole_corpus(inp)
    # ground truth lives in the generator; recover it by re-running generation (deterministic) into memory
    truth = _truth(work)
    items = [(p, str(p.relative_to(inp))) for p in sorted(inp.rglob("*")) if p.is_file()]
    secret = b"attempt3-synthetic-secret-000000"
    st = sc.build_scope(work / "map.sqlite", secret, items)
    scope = sc.Scope(work / "map.sqlite", secret, st["digest"])
    outd = work / "out"
    status = defaultdict(int)
    final = {}
    for p, rel in items:
        dst = outd / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        r = twopass.transform_file(_NoCatalog(), p, dst, rel, overwrite=True, scope=scope)
        status[f"{r.format}:{r.status}:{(r.reason or '').split(':')[0]}"] += 1
        if r.status != "transformed" or not dst.exists():
            shutil.copyfile(p, dst)  # baseline passthrough counts against us
        final[rel] = dst
    # ---- PDF occurrence removal
    occ_tot = occ_rem = 0
    repl = {}  # (rel, idx) -> replacement text
    for rel, occs in truth.items():
        out = final[rel]
        d = pymupdf.open(out)
        full = _doc_text(out)
        for i, (ident, value, pno, rect) in enumerate(occs):
            occ_tot += 1
            if pno < 0:
                got = (d.metadata or {}).get("author", "")
            else:
                pg = d[pno]
                got = pg.get_textbox(pymupdf.Rect(rect) + (-2, -1, 40, 1)) if rect else ""
                got = got.strip()
            if _norm(value) not in _norm(got) and _norm(value) not in full:
                occ_rem += 1
                repl[(rel, i)] = (ident, _replacement_token(got))
            else:
                repl[(rel, i)] = (ident, None)
        d.close()
    # ---- cross-document pairs: same identity in two different docs -> same first token of replacement
    by_ident = defaultdict(list)
    for (rel, i), (ident, tok) in repl.items():
        by_ident[ident].append((rel, tok))
    pairs = pairs_ok = 0
    for ident, lst in by_ident.items():
        for (ra, ta), (rb, tb) in combinations(lst, 2):
            if ra == rb:
                continue
            pairs += 1
            pairs_ok += bool(ta and tb and ta == tb)
    trip = trip_ok = 0
    idents = sorted(by_ident)
    for a in idents:
        for b in idents:
            if a == b:
                continue
            for (ra, ta), (rb, tb) in combinations(by_ident[a], 2):
                if ra == rb:
                    continue
                for rc, tc in by_ident[b][:2]:
                    trip += 1
                    trip_ok += bool(ta and ta == tb and tc and tc != ta)
    # ---- controls unchanged in text
    # ---- OLE: original account bytes anywhere in the output file (any encoding)
    ole_tot = ole_rem = 0
    ole_alias = defaultdict(set)
    for p, rel, vals in oles:
        data = final[rel].read_bytes()
        for v in vals:
            ole_tot += 1
            if v.encode("cp1252") not in data and v.encode("utf-16-le") not in data:
                ole_rem += 1
    return {"status": dict(sorted(status.items())), "pdf_occurrences": occ_tot, "pdf_removed": occ_rem,
            "pdf_pairs": pairs, "pdf_pairs_consistent": pairs_ok, "triplets": trip, "triplets_ok": trip_ok,
            "ole_values": ole_tot, "ole_removed": ole_rem}


def _truth(work: Path) -> dict:
    tmp = work / "truth"
    docs_occ = {}
    global Doc
    orig_save = Doc.save

    def save(self, base, author=None):
        out = orig_save(self, base, author)
        docs_occ[self.rel] = list(self.occ)
        return out
    Doc.save = save
    try:
        make_corpus(tmp)
    finally:
        Doc.save = orig_save
    shutil.rmtree(tmp)
    return docs_occ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="a3e2e-") as t:
        res = run(Path(t))
    print(json.dumps(res, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
