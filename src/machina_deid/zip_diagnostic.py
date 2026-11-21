"""Attempt 2c: read-only ZIP residual diagnostic over the frozen catalog (redacted aggregates only).

Selects residual-positive ``.zip`` rows of the reference (attempt-1) ledger deterministically by
``sha256(path)``, re-runs the *current* archive pass 2 into private scratch, and classifies every
remaining reported hit. Nothing but counts and fixed codes is emitted (never paths, member names,
values, pseudonyms or identity indices).

Metrics, kept separate on purpose (semantics differ):

* ``ledger_old_residual_total`` -- attempt-1 ledger ``residual_approved`` (exact old metric).
* ``new_reported_residual_total`` -- what the current pipeline reports (strict-encoding ``_scan``).
* ``new_legacy_scan_total`` -- the same output rescanned with the pre-fix ``errors='ignore'`` scan,
  so the old semantics stay reproducible on new outputs.

Every reported hit (strict scan) lands in exactly one ``evidence`` bucket:

* ``artifact:overlap_longer_value`` -- occurrence lies inside an occurrence of a longer map value
  (the same bytes are already counted for the longer value; provable double count).
* ``artifact:inside_pseudonym`` -- occurrence lies inside a pseudonym the pipeline wrote.
* ``confirmed:step_literal`` / ``confirmed:step_header`` -- inside a parsed STEP string literal.
* ``confirmed:pdf_structured`` -- the independent PDF inspector found that many (capped by raw).
* ``confirmed:text_member`` -- unsupported member that decodes as UTF-8/UTF-16 text.
* ``unknown:raw_only`` -- raw bytes match; not independently proven either way.

``unknown:*`` hits are never claimed real or false. Hits that a parser sees but the raw scan does not
(compressed PDF streams, STEP ``\\X2\\`` encodings) are reported as ``structured_not_raw`` and are not in
any reported total. Memory: one member at a time (bounded by ``ArchiveLimits.max_member_bytes``);
scratch holds one rebuilt archive at a time.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from . import archive as ar
from . import residual_pilot as rp
from . import twopass as tp
from .catalog import IdentityCatalog
from .idmap import IdentityMap

_TYPE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _len_bucket(v: str) -> str:
    n = len(v)
    return "len<=3" if n <= 3 else "len4-5" if n <= 5 else "len6-9" if n <= 9 else "len10+"


def _spans(data: bytes, needle: bytes) -> list[int]:
    out, i = [], data.find(needle)
    while i != -1:
        out.append(i)
        i = data.find(needle, i + 1)
    return out


def _is_word(b: int) -> bool:
    return chr(b).isalnum() if b < 128 else True  # non-ascii lead/trail bytes treated as letters


def hit_evidence(data: bytes, idmap: IdentityMap) -> dict[int, Counter]:
    """Per identity index: provable-artifact / boundary / encoding / casefold classification of the
    strict raw hits (sum of artifact+other keys == strict ``_scan`` count)."""
    strings = list(idmap.strings)
    pseud = [v for v in idmap.strings.values() if v]
    covered: dict[str, list[tuple[int, int]]] = {}
    for enc in ar.SCAN_ENCODINGS:
        iv = []
        for p in pseud:
            try:
                b = p.encode(enc)
            except UnicodeEncodeError:
                continue
            iv += [(s, s + len(b)) for s in _spans(data, b)]
        covered[enc] = iv
    out: dict[int, Counter] = {}
    for i, v in enumerate(strings):
        encs = ar.scan_encodings(v)
        best_enc, best = None, []
        for enc, b in encs.items():
            sp = _spans(data, b)
            if len(sp) > len(best):
                best_enc, best = enc, sp
        if not best:
            continue
        c = Counter()
        nb = len(encs[best_enc])
        longer = []
        for w in strings:
            if len(w) > len(v) and v in w:
                try:
                    lb = w.encode(best_enc)
                except UnicodeEncodeError:
                    continue
                longer += [(s, s + len(lb)) for s in _spans(data, lb)]
        for s in best:
            e = s + nb
            if any(a <= s and e <= b for a, b in longer):
                c["artifact:overlap_longer_value"] += 1
            elif any(a <= s and e <= b for a, b in covered[best_enc]):
                c["artifact:inside_pseudonym"] += 1
            else:
                c["candidate"] += 1
                step = 2 if best_enc == "utf-16-le" else 1
                pre = data[s - step] if s >= step else None
                post = data[e] if e < len(data) else None
                embedded = (pre is not None and _is_word(pre) and _is_word(v[0].encode()[0])) or \
                           (post is not None and _is_word(post) and _is_word(v[-1].encode()[-1]))
                c["boundary:" + ("embedded_in_word" if embedded else "word_bounded")] += 1
        c["encoding:" + ("ascii" if v.isascii() and best_enc == "utf-8" else best_enc)] += len(best)
        c[_len_bucket(v)] += len(best)
        out[i] = c
    return out


# ------------------------------------------------------------------ format-aware location

def step_locations(path: Path, values: list[str]) -> Counter:
    """Occurrence counts of ``values`` by STEP location *type* (decoded literals)."""
    from . import step_data_transform as sdt
    from . import step_transform as st
    out: Counter = Counter()
    try:
        hdr, _ = st.read_step_header(path)
        for s in hdr.strings:
            text, directives = st._split_content(hdr.text[s.start + 1:s.end - 1])
            if directives:
                text = st._decode_directives(text)
            n = sum(text.count(v) for v in values)
            if n:
                out[f"step_header:{s.field or 'other_header_record'}"] += n
    except st.StepError:
        out["step_parse:header_unparseable"] += 1
    try:
        p = sdt.parse(path.read_bytes(), sdt.IDENTITY_SPEC)
        q = sdt.parse(p.text.encode("latin-1"), sdt.DESCRIPTIVE_SPEC)
    except sdt.StepDataError as exc:
        out[f"step_parse:data_unparseable:{rp._code(str(exc))}"] += 1
        return out
    ident = {s.start: s for s in p.sites}
    descr = {s.start for s in q.sites}
    for r in p.records:
        etype = r.types[0] if len(r.types) == 1 and _TYPE.match(r.types[0]) else "complex_or_other"
        for a, b in r.spans:
            raw = p.text[a + 1:b - 1]
            lit = sdt.decode_literal(raw) or raw
            n = sum(lit.count(v) for v in values)
            if not n:
                continue
            if a in ident:
                site = ident[a]
                kind = "undecodable" if site.value is None else "exact" if site.value in values else "substring"
                out[f"step_data:identity_position_{kind}"] += n
            elif a in descr:
                out[f"step_data:descriptive_scoped:{etype}"] += n
            else:
                out[f"step_data:other_entity:{etype}"] += n
    return out


def _text_like(data: bytes) -> bool:
    """Whole member decodes as UTF-8 (or BOM-marked UTF-16) and is >=95% printable."""
    enc = "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    try:
        t = data.decode(enc)
    except UnicodeDecodeError:
        return False
    head = t[:65536]
    return sum(ch.isprintable() or ch in "\r\n\t" for ch in head) >= 0.95 * len(head)


# ------------------------------------------------------------------ per-archive

class _Agg:
    def __init__(self):
        self.c: dict[str, Counter] = defaultdict(Counter)

    def add(self, sect: str, key: str, n: int = 1):
        if n:
            self.c[sect][key] += n


def _member_walk(zf: zipfile.ZipFile, reps: list, limits: ar.ArchiveLimits):
    """Yield ``(report, bytes)`` for every member of the *rebuilt* archive, mirroring ``_rebuild`` order."""
    infos = zf.infolist()
    if len(infos) != len(reps):
        raise ar.ArchiveError("diagnostic_member_mismatch")
    for info, rep in zip(infos, reps):
        data = b"" if info.is_dir() else ar._read_bounded(zf, info, limits.max_member_bytes)
        yield rep, data
        if rep.outcome == "nested_archive":
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                yield from _member_walk(inner, rep.children, limits)


def diagnose_archive(out_zip: Path, res: ar.ArchiveResult, idmap: IdentityMap, data_only: set[str],
                     work: Path, agg: _Agg, limits: ar.ArchiveLimits = ar.ArchiveLimits()) -> None:
    strings = list(idmap.strings)
    values = [v for v in strings if v]
    with zipfile.ZipFile(out_zip) as zf:
        for rep, data in _member_walk(zf, res.members, limits):
            ext = rep.ext if re.fullmatch(r"\.[a-z0-9]{1,8}", rep.ext or "") else ("(none)" if not rep.ext else ".other")
            agg.add("entry_names", "flagged", int(rep.name_contains_identity))
            if rep.is_dir:
                continue
            reported = sum(rep.residual.values())
            legacy = sum(ar._scan_legacy(data, idmap).values()) if rep.outcome != "nested_archive" else 0
            agg.add("totals", "new_legacy_scan_total", legacy)
            agg.add("totals", "new_reported_residual_total", reported)
            if not reported and rep.outcome != "identifiers_unmodifiable":
                continue
            mkey = f"{ext}|{rep.outcome}|{rp._code(rep.reason) or '-'}"
            agg.add("by_member", f"{mkey}|members", 1)
            agg.add("by_member", f"{mkey}|reported_hits", reported)
            agg.add("by_member", f"{mkey}|depth{min(rep.depth, 2)}", 1)
            if rep.outcome == "nested_archive":
                continue
            strict = ar._scan(data, idmap)
            agg.add("totals", "reported_vs_strict_rescan_delta", reported - sum(strict.values()))
            ev = hit_evidence(data, idmap)
            cand_total = 0
            for i, c in ev.items():
                src = "data_position_map" if strings[i] in data_only else "exact_candidate_map"
                for k, n in c.items():
                    agg.add("mechanism", f"{src}|{k}", n)
                    if k.startswith("artifact:"):
                        agg.add("evidence", k, n)
                cand_total += c["candidate"]
            # casefold-only hits: present ignoring case but not exactly (never counted; unknown)
            low = data.lower()
            cf = sum(max(0, low.count(v.lower().encode("utf-8")) - data.count(v.encode("utf-8")))
                     for v in values if v.lower() != v or v.upper() != v)
            agg.add("unknowns", "casefold_only_utf8_occurrences", cf)
            confirmed = 0
            if rep.ext in (".step", ".stp"):
                p = work / "m.stp"
                p.write_bytes(data)
                try:
                    loc = step_locations(p, values)
                finally:
                    p.unlink(missing_ok=True)
                for k, n in loc.items():
                    agg.add("step_location", k, n)
                confirmed = sum(n for k, n in loc.items() if k.startswith(("step_header:", "step_data:")))
                ctag = "confirmed:step_literal"
            elif rep.ext == ".pdf":
                from .pdf_residual import inspect_pdf
                p = work / "m.pdf"
                p.write_bytes(data)
                try:
                    ind = inspect_pdf(p, values)
                finally:
                    p.unlink(missing_ok=True)
                agg.add("pdf_stage", f"{rp._code(rep.reason) or rep.outcome}|inspect_ok={ind.ok}", 1)
                for k, n in ind.residual.items():
                    agg.add("pdf_location", rp._code(k), n)
                confirmed = ind.total if ind.ok else 0
                ctag = "confirmed:pdf_structured"
            elif _text_like(data):
                confirmed, ctag = cand_total, "confirmed:text_member"
            else:
                ctag = ""
            take = min(confirmed, cand_total)
            if ctag:
                agg.add("evidence", ctag, take)
            agg.add("evidence", "unknown:raw_only", cand_total - take)
            agg.add("unknowns", "structured_not_raw", max(0, confirmed - cand_total))


# ------------------------------------------------------------------ pilot

def select_zip_samples(ledger, limit: int) -> list[tuple[str, str, int]]:
    rows = ledger.execute("SELECT o.path, o.digest, d.residual_approved FROM output o JOIN output_detail d "
                          "USING(path) WHERE o.status='transformed' AND d.residual_approved > 0").fetchall()
    rows = [r for r in rows if PurePosixPath(r[0]).suffix.lower() == ".zip"]
    rows.sort(key=lambda t: rp._key(t[0]))
    return rows[:limit]


def run(root: Path, attempt: str, ref: str, scratch: Path, limit: int, keep: bool = False) -> dict:
    root = Path(root)
    rp._guard_scratch(scratch, root)
    private, ref_private = root / "ops" / attempt, root / "ops" / ref
    ref_corpus, inp = root / "attempts" / ref / "corpus", root / "input"
    ledger = rp._ro(ref_private / "ledger.sqlite")
    frozen = ledger.execute("SELECT value FROM runner_meta WHERE key='catalog_digest'").fetchone()
    from .production import Config, catalog_digest
    if frozen is None or catalog_digest(Config(root=root, attempt=attempt)) != frozen[0]:
        ledger.close()
        raise SystemExit("catalog is not the frozen reference catalog (run `adopt` first)")
    cat = IdentityCatalog(private / "catalog.sqlite", (private / "seed").read_bytes(), read_only=True)
    agg = _Agg()
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="zipdiag-", dir=scratch) as w:
            work = Path(w)
            for rel, old_digest, old_res in select_zip_samples(ledger, limit):
                t = agg.c["totals"]
                t["archives"] += 1
                t["ledger_old_residual_total"] += old_res
                src, ref_out = inp / rel, ref_corpus / rel
                if not src.is_file():
                    t["input_missing"] += 1
                    continue
                before = rp._sha(src)
                t["ref_output_matches_ledger"] += int(ref_out.is_file() and rp._sha(ref_out) == old_digest)
                found = tp.discover_file(src, rel)
                cands = [c for d in found for c in d.candidates]
                idmap, data_only, _ = tp.zip_member_map(cat, cands)
                for d in found:
                    if d.status == "failed":
                        agg.add("discovery", f"failed:{rp._code(d.reason)}")
                if not idmap.strings:
                    t["no_approved_values"] += 1
                    continue
                dst = work / "out.zip"
                try:
                    r = ar.transform_zip(src, dst, idmap)
                    t["source_preserved"] += int(r.source_unchanged and rp._sha(src) == before)
                    if not r.ok:
                        agg.add("archive_failed", rp._code(r.reason))
                        continue
                    t["archives_still_residual"] += int(bool(r.residual_members))
                    t["identical_to_attempt1(hardlinkable)"] += int(r.output_sha256 == old_digest)
                    try:
                        diagnose_archive(dst, r, idmap, data_only, work, agg)
                    except Exception as exc:  # noqa: BLE001  diagnostic never aborts the run
                        agg.add("diagnostic_error", type(exc).__name__)
                finally:
                    dst.unlink(missing_ok=True)
    finally:
        cat.close()
        ledger.close()
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)
    return {"catalog_digest_verified": True, "limit": limit,
            **{k: dict(sorted(v.items())) for k, v in sorted(agg.c.items())}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--attempt", default="attempt-2")
    ap.add_argument("--ref", default="attempt-1")
    ap.add_argument("--scratch", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=30, help="residual-positive ZIPs (deterministic order)")
    ap.add_argument("--keep-scratch", action="store_true")
    a = ap.parse_args(argv)
    json.dump(run(a.root, a.attempt, a.ref, a.scratch, a.limit, a.keep_scratch), sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
