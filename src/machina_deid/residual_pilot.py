"""Attempt 2b: read-only residual diagnostic / local pilot over the frozen catalog.

Selects deterministic, representative residual-positive samples from the attempt-1 ledger
(``output`` JOIN ``output_detail``: status ``transformed`` and ``residual_approved > 0``), plus an equal-sized
residual-free control set, ordered by ``sha256(path)`` (stable, spread, independent of directory layout).
For every sample it

* verifies the attempt-1 output still hashes to the attempt-1 ledger digest (reference integrity),
* hashes the input before/after (source preservation),
* re-runs the *current* pass-2 transform into a private scratch directory outside the package/corpus,
* attributes approved-value occurrences in the input to a location *type* (never the value),
* compares old vs new residual counts and whether the new output is byte-identical to attempt 1
  (i.e. would be hardlinked by ``run --reuse-from``).

Only aggregate counts and fixed reason codes are emitted: never paths, names, values or pseudonyms.
Nothing under ``input/``, ``attempts/`` or ``ops/`` is opened for writing; sqlite is opened ``mode=ro``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from . import step_data_transform as sdt
from . import step_transform as st
from . import twopass as tp
from .catalog import IdentityCatalog

_CODE = re.compile(r"^[a-z0-9_:.\-]{1,80}$")
_TYPE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PILOT_EXTS = (".step", ".stp", ".zip", ".dxf", ".pdf")
NEW_EXTS = (".svg", ".stl")  # attempt-1 unsupported_passthrough rows now read for pass-2 lookups


def _code(s: str) -> str:
    """Fixed reason codes only; anything else is collapsed (could carry content)."""
    return s if _CODE.match(s or "") else "other_reason"


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------ attribution


def attribute_step(path: Path, cat: IdentityCatalog) -> Counter:
    """Location *types* of approved-value occurrences in a STEP file (input view). Keys only."""
    out: Counter = Counter()
    try:
        found = tp.discover_file(path, path.name)
    except Exception:  # noqa: BLE001
        out["discovery_error"] += 1
        return out
    cands = [c for d in found for c in d.candidates]
    idmap, _ = tp.build_identity_map(cat, cands)
    org_map, same = tp.build_org_map(cat, cands)
    values = [v for v in {*idmap.strings, *org_map} if v]
    if same:
        out["data:approved_pseudonym_equals_value"] += same
    # attempt-1 typed gate: approved value at ORGANIZATION.name not approved under kind org
    for c in cands:
        if c.location.startswith("step:DATA.ORGANIZATION.name") and c.value in org_map \
                and cat.approved_pseudonym(c.value, kind="org") is None:
            out["data:org_name_non_org_kind(a1_typed_gate)"] += 1
    if not values:
        return out
    try:
        hdr, _ = st.read_step_header(path)
    except st.StepError:
        out["header_unparseable"] += 1
        return out
    for s in hdr.strings:
        raw = hdr.text[s.start + 1:s.end - 1]
        text, directives = st._split_content(raw)
        if directives:
            text = st._decode_directives(text)
        n = sum(text.count(v) for v in values)
        if n:
            field = s.field or "other_header_record"
            out[f"header:{field}{':directive' if directives else ''}"] += n
    try:
        p = sdt.parse(path.read_bytes(), sdt.IDENTITY_SPEC)
        q = sdt.parse(p.text.encode("latin-1"), sdt.DESCRIPTIVE_SPEC)
    except sdt.StepDataError as exc:
        out[f"data_unparseable:{_code(str(exc))}"] += 1
        return out
    ident = {s.start: s for s in p.sites}
    descr = {s.start for s in q.sites}
    for s in p.sites:
        if s.value is None:
            carries = sdt.raw_contains_any(p.text[s.start + 1:s.end - 1], values)
            out["data:identity_undecodable_" + ("with_value" if carries else "without_value")] += 1
    for r in p.records:
        etype = r.types[0] if len(r.types) == 1 and _TYPE.match(r.types[0]) else "complex_or_other"
        for a, b in r.spans:
            raw = p.text[a + 1:b - 1]
            lit = sdt.decode_literal(raw) or raw
            if not any(v in lit for v in values):
                continue
            if a in ident:
                kind = "exact" if ident[a].value in values else "substring"
                out[f"data:identity_position_{kind}"] += 1
            elif a in descr:
                bh = sdt.boundary_hits(lit, values)
                out["data:descriptive_scoped_" + ("boundary" if bh else "embedded_no_boundary")] += 1
            else:
                out[f"data:other_entity:{etype}"] += 1
    return out


# ------------------------------------------------------------------ pilot


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _key(rel: str) -> str:
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()


def select_samples(ledger: sqlite3.Connection, per_ext: int, exts=PILOT_EXTS) -> dict[str, list[tuple]]:
    """``{"residual"|"control": [(rel, out_digest, old_residual, old_fields)]}`` deterministic by sha256(path)."""
    rows = ledger.execute("SELECT o.path, o.status, o.digest, d.residual_approved, d.fields_replaced FROM output o "
                          "JOIN output_detail d USING(path) "
                          "WHERE o.status IN ('transformed', 'unsupported_passthrough')").fetchall()
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for rel, status, dig, res, fields in rows:
        ext = PurePosixPath(rel).suffix.lower()
        if status == "transformed" and ext in exts:
            groups[("residual" if res > 0 else "control", ext)].append((rel, dig, res, fields))
        elif status == "unsupported_passthrough" and ext in NEW_EXTS:
            groups[("newly_supported", ext)].append((rel, dig, res, fields))
    out: dict[str, list] = {"residual": [], "control": [], "newly_supported": []}
    for (cls, _), items in sorted(groups.items()):
        items.sort(key=lambda t: _key(t[0]))
        out[cls] += items[: per_ext if cls != "control" else max(1, per_ext // 4)]
    return out


def _guard_scratch(scratch: Path, root: Path) -> None:
    s = scratch.resolve()
    for forbidden in (root / "input", root / "attempts", root / "ops"):
        f = forbidden.resolve()
        if s == f or f in s.parents:
            raise SystemExit("scratch directory must be outside input/, attempts/ and ops/")
    if scratch.exists() and any(scratch.iterdir()):
        raise SystemExit("scratch directory must be empty or absent")


def run_pilot(root: Path, attempt: str, ref: str, scratch: Path, per_ext: int, keep: bool = False) -> dict:
    root = Path(root)
    _guard_scratch(scratch, root)
    private, ref_private = root / "ops" / attempt, root / "ops" / ref
    ref_corpus, inp = root / "attempts" / ref / "corpus", root / "input"
    ledger = _ro(ref_private / "ledger.sqlite")
    frozen = ledger.execute("SELECT value FROM runner_meta WHERE key='catalog_digest'").fetchone()
    from .production import Config, catalog_digest
    if frozen is None or catalog_digest(Config(root=root, attempt=attempt)) != frozen[0]:
        raise SystemExit("catalog is not the frozen reference catalog (run `adopt` first)")
    cat = IdentityCatalog(private / "catalog.sqlite", (private / "seed").read_bytes(), read_only=True)
    samples = select_samples(ledger, per_ext)
    scratch.mkdir(parents=True, exist_ok=True)
    report: dict = {"catalog_digest_verified": True, "per_ext_limit": per_ext, "classes": {}}
    try:
        with tempfile.TemporaryDirectory(prefix="pilot-", dir=scratch) as work:
            for cls, items in samples.items():
                agg: dict[str, Counter] = defaultdict(Counter)
                attr: dict[str, Counter] = defaultdict(Counter)
                for i, (rel, old_digest, old_res, old_fields) in enumerate(items):
                    ext = PurePosixPath(rel).suffix.lower()
                    a, c = attr[ext], agg[ext]
                    c["samples"] += 1
                    src, ref_out = inp / rel, ref_corpus / rel
                    if not src.is_file():
                        c["input_missing"] += 1
                        continue
                    before = _sha(src)
                    c["ref_output_matches_ledger"] += int(ref_out.is_file() and _sha(ref_out) == old_digest)
                    dst = Path(work) / f"{i}{ext}"
                    o = tp.transform_file(cat, src, dst, rel)
                    c["source_preserved"] += int(o.source_unchanged and _sha(src) == before)
                    c["old_residual_total"] += old_res
                    c["old_files_with_residual"] += int(old_res > 0)
                    c["old_fields_total"] += old_fields
                    c[f"new_status:{_code(o.status)}"] += 1
                    if o.status != "transformed":
                        c[f"new_reason:{_code(o.reason)}"] += 1
                    c["new_residual_total"] += o.residual_approved
                    c["new_files_with_residual"] += int(o.residual_approved > 0)
                    c["new_fields_total"] += o.fields_replaced
                    c["new_fully_clean"] += int(o.fully_clean)
                    for k, v in o.replacement_counts().items():
                        if v and k != "total_verified_identity_fields_changed":
                            c[f"new_changes:{k}"] += v
                    for k, v in o.independent_residual.items():
                        c[f"new_independent:{_code(k)}"] += v
                    if o.output_written:
                        c["identical_to_attempt1(hardlinkable)"] += int(_sha(dst) == old_digest)
                        dst.unlink()
                    if ext in (".step", ".stp"):
                        a.update(attribute_step(src, cat))
                report["classes"][cls] = {ext: {"counts": dict(sorted(agg[ext].items())),
                                                "input_attribution": dict(sorted(attr[ext].items()))}
                                          for ext in sorted(agg)}
    finally:
        cat.close()
        ledger.close()
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--attempt", default="attempt-2", help="adopted state holding the frozen catalog copy")
    ap.add_argument("--ref", default="attempt-1", help="reference attempt (ledger + corpus, read only)")
    ap.add_argument("--scratch", required=True, type=Path, help="empty dir outside input/attempts/ops")
    ap.add_argument("--per-ext", type=int, default=100)
    ap.add_argument("--keep-scratch", action="store_true")
    a = ap.parse_args(argv)
    json.dump(run_pilot(a.root, a.attempt, a.ref, a.scratch, a.per_ext, a.keep_scratch), sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
