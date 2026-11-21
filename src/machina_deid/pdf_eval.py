"""Independent evaluator for PDF de-identification against private human labels.

Deliberately does **not** import the pipeline's discovery/rewrite modules: it re-extracts characters with its
own code, so a pipeline bug cannot also hide in the evaluator. Inputs (all private, local):

* ``labels.jsonl`` - one labelled occurrence per line::

      {"doc": "rel/path.pdf", "page": 0, "bbox": [x0, y0, x1, y1], "text": "Jane Doe",
       "entity": "E17", "kind": "person", "family": "optional", "out_doc": "optional renamed output path"}

  ``{"type": "keep", ...}`` lines mark engineering text that must stay byte-identical in place.
  Coordinates are PyMuPDF text space (unrotated page), as written by ``pdf_lab propose``.
* input root, output root, optional pipeline ``trace.jsonl`` (value-free edit records) for failure classes.

Metrics (an occurrence succeeds only when the original is absent from its region, a different non-empty
replacement is extractable there *and* renders; a fallback/unchanged copy or a missing output always fails):

* ``scoped_removal`` - successful occurrences / labelled occurrences.
* ``same_identity`` - pairs of occurrences of one entity that both succeed with the same pseudonym token.
* ``cross_document_triplets`` - triples of one entity's occurrences in 3 distinct documents, all successful and
  equal (proxy for drawing/model/document consistency).
* ``distinctness`` - pairs of different entities that received the same token (collisions; lower is better).
* ``engineering`` - per document: page boxes/rotation, vector drawings, images, keep-labels and text outside
  label regions.

Failure classes: ``missing_output``, ``fallback_copy``, ``discovery`` (no pipeline edit/finding there),
``resolution`` (found but unresolved/ambiguous), ``rewrite`` (pipeline edit failed), ``validation`` (pipeline
claimed verified but the evaluator disagrees), ``unclassified`` (no trace given).
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pymupdf

_TOKEN = re.compile(r"(?:\b[A-Za-z]{2,16}-|\buser-|\bdomain-|\+000-)([0-9A-Fa-f]{5,32})\b")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s).casefold())


def canonical_token(replacement: str) -> str:
    """Pseudonym token (hex) if the replacement carries one, else the normalized replacement text."""
    m = _TOKEN.search(replacement)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b([0-9a-f]{5,32})@", replacement)
    return m.group(1).upper() if m else _norm(replacement)


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ independent extraction


def _chars(page) -> list[tuple[str, tuple[float, float, float, float]]]:
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES)
    out = []
    for b in raw.get("blocks", []):
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    if ch.get("c", "").strip():
                        out.append((ch["c"], tuple(ch["bbox"])))
    return out


def _center_in(b, r: pymupdf.Rect) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1


def _q(b) -> tuple:
    return tuple(round(x * 2) / 2 for x in b)


def _region(bbox) -> pymupdf.Rect:
    r = pymupdf.Rect(bbox)
    s, long = min(r.width, r.height), max(r.width, r.height)
    g = max(1.2 * s, 0.6 * long)  # along the text: a replacement may be longer/shorter than the original
    return r + (-g, -0.35 * s, g, 0.35 * s) if r.width >= r.height else r + (-0.35 * s, -g, 0.35 * s, g)


def _ink(page, rect: pymupdf.Rect) -> bool:
    clip = (rect * page.rotation_matrix) & page.rect
    if clip.is_empty or clip.width < 0.5 or clip.height < 0.5:
        return False
    zoom = max(1.0, min(8.0, 24.0 / max(min(clip.width, clip.height), 1e-3)))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    d = pix.samples
    return bool(d) and max(d) - min(d) >= 64


def _drawings(page) -> list:
    try:
        return [(d.get("type"), _q(d.get("rect", (0, 0, 0, 0))), len(d.get("items", ()))) for d in page.get_drawings()]
    except Exception:  # noqa: BLE001
        return ["error"]


def _images(page) -> list:
    try:
        return sorted(_q(i["bbox"]) for i in page.get_image_info())
    except Exception:  # noqa: BLE001
        return ["error"]


# ------------------------------------------------------------------ data


@dataclass
class Label:
    doc: str
    page: int
    bbox: tuple
    text: str = field(repr=False)
    entity: str = ""
    kind: str = ""
    family: str = ""
    out_doc: str = ""
    type: str = "identity"


def load_labels(path: str | Path) -> list[Label]:
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        d = json.loads(line)
        if d.get("status") in ("rejected", "proposed"):
            continue  # only confirmed labels are evaluated
        try:
            out.append(Label(d["doc"], int(d.get("page", 0)), tuple(float(x) for x in d["bbox"]), d.get("text", ""),
                             str(d.get("entity", "")), d.get("kind", ""), d.get("family", ""), d.get("out_doc", ""),
                             d.get("type", "identity")))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"labels line {n}: {type(exc).__name__}") from None
    return out


def load_trace(path: str | Path | None) -> dict[str, list[dict]]:
    by_doc: dict[str, list[dict]] = defaultdict(list)
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                by_doc[d["doc"]].append(d)
    return by_doc


def family_of(label: Label) -> str:
    if label.family:
        return label.family
    p = PurePosixPath(label.doc.replace("\\", "/"))
    return str(p.parent) if str(p.parent) not in ("", ".") else p.stem.split("_")[0].split("-")[0].lower()


def holdout_split(labels: list[Label], input_root: Path, seed: bytes, frac: float) -> dict[str, str]:
    """doc -> 'dev' | 'holdout'. Families are joined with every family that shares a byte-identical input
    (duplicates), so no duplicate can leak across the split; the component is assigned by keyed hash."""
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_sha: dict[str, str] = {}
    fam_of_doc = {}
    for lb in labels:
        fam_of_doc.setdefault(lb.doc, "fam:" + family_of(lb))
    for doc, fam in fam_of_doc.items():
        find(fam)
        p = input_root / doc
        if p.is_file():
            s = _sha(p)
            if s in by_sha:
                parent[find(fam)] = find(by_sha[s])
            else:
                by_sha[s] = fam
    out = {}
    for doc, fam in fam_of_doc.items():
        comp = find(fam)
        h = hmac.new(seed, comp.encode(), hashlib.sha256).digest()
        out[doc] = "holdout" if int.from_bytes(h[:8], "big") / 2**64 < frac else "dev"
    return out


# ------------------------------------------------------------------ evaluation


@dataclass
class Occ:
    label: Label
    ok: bool = False
    cls: str = ""
    token: str = ""


def _classify(lb: Label, trace: list[dict], evaluator_ok: bool) -> str:
    if trace is None:
        return "unclassified"
    r = pymupdf.Rect(lb.bbox)
    hits = [t for t in trace if t.get("page") == lb.page and pymupdf.Rect(t["rect"]).intersects(r)
            and abs(pymupdf.Rect(t["rect"]) & r) >= 0.3 * min(abs(r), abs(pymupdf.Rect(t["rect"])) or 1)]
    if not hits:
        return "discovery"
    st = [t.get("status", "") for t in hits]
    if any(s == "verified" for s in st):
        return "validation"  # pipeline claimed success; evaluator found the original or no replacement
    if any(s.startswith("resolution") for s in st):
        return "resolution"
    return "rewrite"


def evaluate(labels: list[Label], input_root: str | Path, output_root: str | Path, *,
             trace_path: str | Path | None = None, split: dict[str, str] | None = None,
             max_pairs: int = 2000, rng_seed: int = 0) -> dict:
    input_root, output_root = Path(input_root), Path(output_root)
    traces = load_trace(trace_path) if trace_path else None
    by_doc: dict[str, list[Label]] = defaultdict(list)
    for lb in labels:
        by_doc[lb.doc].append(lb)
    occs: list[Occ] = []
    eng: dict[str, dict] = {}
    for doc, lbs in sorted(by_doc.items()):
        src = input_root / doc
        dst = output_root / (lbs[0].out_doc or doc)
        ident = [lb for lb in lbs if lb.type != "keep"]
        if not dst.is_file():
            occs += [Occ(lb, False, "missing_output") for lb in ident]
            eng[doc] = {"ok": False, "reason": "missing_output"}
            continue
        if src.is_file() and _sha(src) == _sha(dst):
            occs += [Occ(lb, False, "fallback_copy") for lb in ident]
            eng[doc] = {"ok": True, "reason": "unchanged_copy"}
            continue
        din, dout = pymupdf.open(src), pymupdf.open(dst)
        try:
            e = {"ok": True, "reason": ""}
            if din.page_count != dout.page_count:
                e = {"ok": False, "reason": "page_count"}
            trace = traces.get(doc, []) if traces is not None else None
            for pno in range(min(din.page_count, dout.page_count)):
                pi, po = din[pno], dout[pno]
                if (tuple(pi.mediabox), tuple(pi.cropbox), pi.rotation) != (tuple(po.mediabox), tuple(po.cropbox), po.rotation):
                    e = {"ok": False, "reason": "page_geometry"}
                if _drawings(pi) != _drawings(po):
                    e = {"ok": False, "reason": "vector_drawings"}
                if _images(pi) != _images(po):
                    e = {"ok": False, "reason": "images"}
                ci, co = _chars(pi), _chars(po)
                plabels = [lb for lb in lbs if lb.page == pno]
                regions = [_region(lb.bbox) for lb in plabels if lb.type != "keep"]
                outside_in = Counter((c, _q(b)) for c, b in ci if not any(_center_in(b, r) for r in regions))
                outside_out = Counter((c, _q(b)) for c, b in co if not any(_center_in(b, r) for r in regions))
                e.setdefault("chars_changed_outside_labels", 0)
                e["chars_changed_outside_labels"] += sum((outside_in - outside_out).values())
                for lb in plabels:
                    if lb.type == "keep":
                        r = pymupdf.Rect(lb.bbox) + (-0.5, -0.5, 0.5, 0.5)
                        if sorted((c, _q(b)) for c, b in ci if _center_in(b, r)) != \
                                sorted((c, _q(b)) for c, b in co if _center_in(b, r)):
                            e = {**e, "ok": False, "reason": "keep_region_changed"}
                        continue
                    reg, lab = _region(lb.bbox), pymupdf.Rect(lb.bbox) + (-0.5, -0.5, 0.5, 0.5)
                    context = Counter((c, _q(b)) for c, b in ci if _center_in(b, reg) and not _center_in(b, lab))
                    rep_chars = []
                    for c, b in co:
                        if _center_in(b, reg):
                            k = (c, _q(b))
                            if context[k]:
                                context[k] -= 1
                            else:
                                rep_chars.append(c)
                    region_out = "".join(c for c, b in co if _center_in(b, reg))
                    replacement = "".join(rep_chars)
                    absent = _norm(lb.text) not in _norm(region_out)
                    ok = absent and bool(_norm(replacement)) and _norm(replacement) != _norm(lb.text) \
                        and _ink(po, pymupdf.Rect(lb.bbox))
                    cls = "" if ok else _classify(lb, trace, ok)
                    occs.append(Occ(lb, ok, cls, canonical_token(replacement) if ok else ""))
            e.setdefault("chars_changed_outside_labels", 0)
            eng[doc] = e
        finally:
            din.close()
            dout.close()
    return _aggregate(occs, eng, split, max_pairs, rng_seed)


def _aggregate(occs: list[Occ], eng: dict, split, max_pairs: int, rng_seed: int) -> dict:
    groups = {"all": occs}
    if split:
        for name in ("dev", "holdout"):
            groups[name] = [o for o in occs if split.get(o.label.doc) == name]
    out = {}
    for name, os_ in groups.items():
        out[name] = _metrics(os_, {d: v for d, v in eng.items() if any(o.label.doc == d for o in os_) or name == "all"},
                             max_pairs, rng_seed)
    return out


def _metrics(occs: list[Occ], eng: dict, max_pairs: int, rng_seed: int) -> dict:
    rng = random.Random(rng_seed)
    n = len(occs)
    ok = sum(o.ok for o in occs)
    fails = Counter(o.cls for o in occs if not o.ok)
    by_kind = defaultdict(lambda: [0, 0])
    for o in occs:
        by_kind[o.label.kind or "?"][0] += o.ok
        by_kind[o.label.kind or "?"][1] += 1
    ents: dict[str, list[Occ]] = defaultdict(list)
    for o in occs:
        if o.label.entity:
            ents[o.label.entity].append(o)
    pairs = [p for e in ents.values() for p in itertools.combinations(e, 2)]
    if len(pairs) > max_pairs:
        pairs = rng.sample(pairs, max_pairs)
    pair_ok = sum(a.ok and b.ok and a.token == b.token for a, b in pairs)
    trip = []
    for e in ents.values():
        by_d = defaultdict(list)
        for o in e:
            by_d[o.label.doc].append(o)
        if len(by_d) >= 3:
            for docs in itertools.combinations(sorted(by_d), 3):
                trip.append(tuple(rng.choice(by_d[d]) for d in docs))
    if len(trip) > max_pairs:
        trip = rng.sample(trip, max_pairs)
    trip_ok = sum(all(o.ok for o in t) and len({o.token for o in t}) == 1 for t in trip)
    tok_ents: dict[str, set] = defaultdict(set)
    for o in occs:
        if o.ok and o.label.entity:
            tok_ents[o.token].add(o.label.entity)
    collisions = sum(len(s) - 1 for s in tok_ents.values() if len(s) > 1)
    eng_ok = sum(v.get("ok", False) for v in eng.values())
    return {
        "occurrences": n,
        "scoped_removal": {"ok": ok, "total": n, "rate": round(ok / n, 4) if n else None},
        "failures": dict(sorted(fails.items())),
        "by_kind": {k: {"ok": v[0], "total": v[1]} for k, v in sorted(by_kind.items())},
        "same_identity": {"ok": pair_ok, "total": len(pairs), "rate": round(pair_ok / len(pairs), 4) if pairs else None},
        "cross_document_triplets": {"ok": trip_ok, "total": len(trip),
                                    "rate": round(trip_ok / len(trip), 4) if trip else None},
        "distinctness_collisions": collisions,
        "engineering": {"docs_ok": eng_ok, "docs": len(eng),
                        "reasons": dict(Counter(v.get("reason", "") for v in eng.values() if not v.get("ok"))),
                        "chars_changed_outside_labels": sum(v.get("chars_changed_outside_labels", 0) for v in eng.values()),
                        "unchanged_copies": sum(v.get("reason") == "unchanged_copy" for v in eng.values())},
    }
