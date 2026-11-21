"""Phase 5B: analysis-only STEP (ISO 10303-21) entity/reference graph.

This module never modifies STEP data. It performs a bounded lexical scan of
the DATA section and emits a machine-readable, *redacted* report: raw string
values are never included -- only salted, truncated HMAC digests and lengths.

Candidate classification is a heuristic hint for later review; it makes no
precision/recall claims.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field

REPORT_VERSION = "step-analysis/1"
HASH_HEX_LEN = 16

# Known identity-bearing entity types -> parameter indexes (top level) that
# are likely to carry identifying strings. Index meanings follow AP203/AP214.
IDENTITY_FIELDS: dict[str, frozenset[int]] = {
    "PRODUCT": frozenset({0, 1, 2}),                      # id, name, description
    "PRODUCT_DEFINITION": frozenset({0, 1}),              # id, description
    "PRODUCT_DEFINITION_FORMATION": frozenset({0, 1}),
    "PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE": frozenset({0, 1}),
    "ORGANIZATION": frozenset({0, 1, 2}),                 # id, name, description
    "PERSON": frozenset({0, 1, 2, 3, 4, 5}),              # id, last, first, middle, prefix, suffix
    "PERSON_AND_ORGANIZATION": frozenset(),               # refs only
    "SECURITY_CLASSIFICATION": frozenset({0, 1}),         # name, purpose
    "APPROVAL": frozenset({1}),
    "DOCUMENT": frozenset({0, 1, 2}),
    "ADDRESS": frozenset(range(12)),
    "PERSONAL_ADDRESS": frozenset(range(12)),
    "ORGANIZATIONAL_ADDRESS": frozenset(range(12)),
    "APPLICATION_CONTEXT": frozenset({0}),
}
RELEVANT_TYPES = frozenset({
    "PRODUCT", "PRODUCT_DEFINITION", "ORGANIZATION", "PERSON",
    "PERSON_AND_ORGANIZATION", "SECURITY_CLASSIFICATION",
}) | frozenset(IDENTITY_FIELDS)
# Types whose string fields are geometry/engineering labels, not identities.
ENGINEERING_TYPES = frozenset({
    "CARTESIAN_POINT", "DIRECTION", "VECTOR", "AXIS2_PLACEMENT_3D", "LINE",
    "CIRCLE", "PLANE", "EDGE_CURVE", "VERTEX_POINT", "ORIENTED_EDGE",
    "EDGE_LOOP", "FACE_BOUND", "FACE_OUTER_BOUND", "ADVANCED_FACE",
    "CLOSED_SHELL", "MANIFOLD_SOLID_BREP", "B_SPLINE_CURVE_WITH_KNOTS",
    "B_SPLINE_SURFACE_WITH_KNOTS", "CYLINDRICAL_SURFACE", "SHAPE_REPRESENTATION",
    "ADVANCED_BREP_SHAPE_REPRESENTATION", "GEOMETRIC_REPRESENTATION_CONTEXT",
    "UNCERTAINTY_MEASURE_WITH_UNIT", "LENGTH_MEASURE_WITH_UNIT",
    "SHAPE_DEFINITION_REPRESENTATION", "PRODUCT_DEFINITION_SHAPE",
    "COLOUR_RGB", "STYLED_ITEM", "PRESENTATION_STYLE_ASSIGNMENT",
})


class StepAnalysisError(ValueError):
    pass


@dataclass(frozen=True)
class AnalysisLimits:
    max_bytes: int = 64 * 1024 * 1024
    max_entities: int = 2_000_000
    max_string_len: int = 4096
    max_nodes: int = 200_000
    max_edges: int = 2_000_000


@dataclass
class _Record:
    eid: int
    types: list[str]
    strings: list[tuple[str, str, int, bool]] = field(default_factory=list)  # (path, type, len, truncated) + raw kept separately
    raws: list[str] = field(default_factory=list)
    refs: list[tuple[str, int]] = field(default_factory=list)
    # byte/char offsets (opening quote, just past closing quote), parallel to ``strings``
    spans: list[tuple[int, int]] = field(default_factory=list)
    arity: list[int] = field(default_factory=list)  # top-level parameter count per type
    start: int = 0  # offset of '#'
    end: int = 0  # offset just past ';'


class _Scanner:
    """Lexical scanner over the DATA section of a Part 21 file."""

    def __init__(self, text: str, limits: AnalysisLimits):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.lim = limits
        self.issues: list[str] = []
        self.strings_truncated = 0

    # -- low level ----------------------------------------------------------
    def skip_ws(self) -> None:
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "/" and s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j < 0:
                    self.issues.append("unterminated_comment")
                    self.i = n
                    return
                self.i = j + 2
            else:
                return

    def read_string(self) -> tuple[str, int, bool]:
        """At an opening quote; return (raw-prefix, full length, truncated)."""
        s, n = self.s, self.n
        self.i += 1
        buf: list[str] = []
        length = 0
        cap = self.lim.max_string_len
        while self.i < n:
            c = s[self.i]
            if c == "'":
                if self.i + 1 < n and s[self.i + 1] == "'":
                    c = "'"
                    self.i += 2
                else:
                    self.i += 1
                    trunc = length > cap
                    if trunc:
                        self.strings_truncated += 1
                    return "".join(buf), length, trunc
            else:
                self.i += 1
            length += 1
            if length <= cap:
                buf.append(c)
        raise StepAnalysisError("unterminated_string")

    def find_data_section(self) -> bool:
        """Advance to just after 'DATA;' (skipping strings/comments)."""
        s, n = self.s, self.n
        while self.i < n:
            self.skip_ws()
            if self.i >= n:
                break
            c = s[self.i]
            if c == "'":
                self.read_string()
            elif s.startswith("DATA", self.i) and (self.i == 0 or not (s[self.i - 1].isalnum() or s[self.i - 1] == "_")):
                j = self.i + 4
                k = j
                while k < n and s[k] in " \t\r\n":
                    k += 1
                if k < n and s[k] in ";(":
                    # DATA; or DATA('name',(...));
                    depth = 0
                    while k < n:
                        ch = s[k]
                        if ch == "'":
                            self.i = k
                            self.read_string()
                            k = self.i
                            continue
                        if ch == "(":
                            depth += 1
                        elif ch == ")":
                            depth -= 1
                        elif ch == ";" and depth == 0:
                            self.i = k + 1
                            return True
                        k += 1
                    return False
                self.i = j
            else:
                self.i += 1
        return False

    def read_keyword(self) -> str:
        s, j = self.s, self.i
        if j < self.n and s[j] == "!":
            j += 1
        while j < self.n and (s[j].isalnum() or s[j] == "_"):
            j += 1
        kw = s[self.i:j].upper()
        self.i = j
        return kw

    def read_int(self) -> int | None:
        j = self.i
        while j < self.n and self.s[j].isdigit():
            j += 1
        if j == self.i:
            return None
        v = int(self.s[self.i:j])
        self.i = j
        return v

    # -- records ------------------------------------------------------------
    def next_record(self) -> _Record | None | str:
        """Return a _Record, None at ENDSEC/EOF, or 'skip' on malformed."""
        self.skip_ws()
        s = self.s
        if self.i >= self.n:
            self.issues.append("missing_endsec")
            return None
        if s.startswith("ENDSEC", self.i):
            return None
        if s[self.i] != "#":
            self._resync()
            self.issues.append("malformed_record")
            return "skip"
        rstart = self.i
        self.i += 1
        eid = self.read_int()
        self.skip_ws()
        if eid is None or self.i >= self.n or s[self.i] != "=":
            self._resync()
            self.issues.append("malformed_record")
            return "skip"
        self.i += 1
        self.skip_ws()
        rec = _Record(eid=eid, types=[], start=rstart)
        if self.i < self.n and s[self.i] == "(":
            # complex entity: ( A(...) B(...) )
            self.i += 1
            k = 0
            while True:
                self.skip_ws()
                if self.i >= self.n:
                    raise StepAnalysisError("unexpected_eof")
                if s[self.i] == ")":
                    self.i += 1
                    break
                kw = self.read_keyword()
                if not kw:
                    raise StepAnalysisError(f"bad_complex_entity:#{eid}")
                rec.types.append(kw)
                self.skip_ws()
                self.parse_params(rec, f"{kw}:", kw)
                k += 1
        else:
            kw = self.read_keyword()
            if not kw:
                self._resync()
                self.issues.append("malformed_record")
                return "skip"
            rec.types.append(kw)
            self.skip_ws()
            self.parse_params(rec, "", kw)
        self.skip_ws()
        if self.i < self.n and s[self.i] == ";":
            self.i += 1
        else:
            self.issues.append("missing_semicolon")
            self._resync()
        rec.end = self.i
        return rec

    def _resync(self) -> None:
        """Skip to after next ';' outside strings/comments."""
        while self.i < self.n:
            self.skip_ws()
            if self.i >= self.n:
                return
            c = self.s[self.i]
            if c == "'":
                self.read_string()
            elif c == ";":
                self.i += 1
                return
            else:
                self.i += 1

    def parse_params(self, rec: _Record, prefix: str, etype: str) -> None:
        s = self.s
        if self.i >= self.n or s[self.i] != "(":
            raise StepAnalysisError(f"expected_params:#{rec.eid}")
        self.i += 1
        path = [0]
        while True:
            self.skip_ws()
            if self.i >= self.n:
                raise StepAnalysisError("unexpected_eof")
            c = s[self.i]
            p = prefix + ".".join(map(str, path))
            if c == "'":
                st = self.i
                raw, ln, tr = self.read_string()
                rec.strings.append((p, etype, ln, tr))
                rec.raws.append(raw)
                rec.spans.append((st, self.i))
            elif c == "#":
                self.i += 1
                ref = self.read_int()
                if ref is None:
                    raise StepAnalysisError(f"bad_reference:#{rec.eid}")
                rec.refs.append((p, ref))
            elif c == "(":
                self.i += 1
                path.append(0)
                continue
            elif c == ")":
                self.i += 1
                if len(path) == 1:
                    rec.arity.append(path[0] + 1)
                path.pop()
                if not path:
                    return
                continue
            elif c == ",":
                self.i += 1
                path[-1] += 1
                continue
            elif c == ";":
                raise StepAnalysisError(f"unbalanced_params:#{rec.eid}")
            elif c.isalpha() or c == "!":
                # typed parameter e.g. LENGTH_MEASURE(1.0); or enum handled below
                self.read_keyword()
                self.skip_ws()
                if self.i < self.n and s[self.i] == "(":
                    self.i += 1
                    path.append(0)
                continue
            elif c == "\"":
                j = s.find("\"", self.i + 1)
                self.i = self.n if j < 0 else j + 1
            else:
                # numbers, enums (.T.), $, *
                j = self.i
                while j < self.n and s[j] not in ",()';#/ \t\r\n":
                    j += 1
                self.i = max(j, self.i + 1)
            # loop continues


def _classify(etype: str, path: str, raw: str) -> tuple[str, str]:
    top = path.split(":")[-1].split(".")[0]
    idx = int(top) if top.isdigit() else -1
    nested = "." in path.split(":")[-1]
    if etype in IDENTITY_FIELDS and idx in IDENTITY_FIELDS[etype] and not nested:
        return "identity_candidate", f"known_field:{etype}[{idx}]"
    if "@" in raw and "." in raw.split("@")[-1]:
        return "identity_candidate", "email_like"
    if etype in ENGINEERING_TYPES:
        return "engineering", "engineering_type"
    if etype in IDENTITY_FIELDS:
        return "unknown", "identity_type_other_field"
    return "unknown", "unclassified"


def analyze_step(data: bytes, *, salt: bytes | None = None,
                 limits: AnalysisLimits | None = None) -> dict:
    """Analyze STEP bytes and return a redacted JSON-serializable report."""
    lim = limits or AnalysisLimits()
    salt_mode = "provided"
    if salt is None:
        salt, salt_mode = os.urandom(32), "ephemeral"
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < 16:
        raise StepAnalysisError("salt must be >= 16 bytes")
    truncation: dict[str, bool | int] = {
        "bytes": False, "entities": False, "nodes": False, "edges": False, "strings": 0,
    }
    src_sha = hashlib.sha256(data).hexdigest()
    src_size = len(data)
    if src_size > lim.max_bytes:
        data = data[:lim.max_bytes]
        truncation["bytes"] = True
    text = data.decode("latin-1")  # Part 21 is 7/8-bit; latin-1 is lossless for scanning

    def h(v: str) -> str:
        return hmac.new(salt, ("machina-deid/step-analysis\0" + v).encode("utf-8"),
                        hashlib.sha256).hexdigest()[:HASH_HEX_LEN]

    sc = _Scanner(text, lim)
    counts: dict[str, int] = {}
    nodes: list[dict] = []
    edges: list[list] = []
    ids: set[int] = set()
    all_refs: set[int] = set()
    duplicates = 0
    entities = 0
    status = "ok"
    try:
        if not sc.find_data_section():
            raise StepAnalysisError("no_data_section")
        while True:
            if entities >= lim.max_entities:
                truncation["entities"] = True
                break
            rec = sc.next_record()
            if rec is None:
                break
            if rec == "skip":
                continue
            entities += 1
            if rec.eid in ids:
                duplicates += 1
            ids.add(rec.eid)
            tname = "+".join(rec.types)
            counts[tname] = counts.get(tname, 0) + 1
            for p, ref in rec.refs:
                all_refs.add(ref)
                if len(edges) >= lim.max_edges:
                    truncation["edges"] = True
                else:
                    edges.append([rec.eid, ref, p])
            relevant = any(t in RELEVANT_TYPES for t in rec.types)
            if relevant or rec.strings:
                if len(nodes) >= lim.max_nodes:
                    truncation["nodes"] = True
                    continue
                fields = []
                for (p, et, ln, tr), raw in zip(rec.strings, rec.raws):
                    cls, why = _classify(et, p, raw)
                    fields.append({"index": p, "entity_type": et, "hash": h(raw), "length": ln,
                                   "truncated": tr, "class": cls, "reason": why})
                nodes.append({"id": rec.eid, "type": tname, "relevant": relevant,
                              "strings": fields, "refs": sorted({r for _, r in rec.refs})})
    except StepAnalysisError as e:
        status = "error"
        sc.issues.append(str(e))
    if truncation["bytes"] and status == "error":
        status = "truncated"
    truncation["strings"] = sc.strings_truncated
    any_trunc = any(bool(v) for v in truncation.values())
    if status == "ok" and any_trunc:
        status = "truncated"
    dangling = sorted(all_refs - ids)
    by_class: dict[str, int] = {}
    for n in nodes:
        for f in n["strings"]:
            by_class[f["class"]] = by_class.get(f["class"], 0) + 1
    return {
        "version": REPORT_VERSION,
        "analysis_only": True,
        "status": status,
        "source": {"sha256": src_sha, "size": src_size, "scanned_bytes": len(data)},
        "hashing": {"algorithm": "hmac-sha256", "hex_len": HASH_HEX_LEN, "salt_mode": salt_mode},
        "limits": {"max_bytes": lim.max_bytes, "max_entities": lim.max_entities,
                   "max_string_len": lim.max_string_len, "max_nodes": lim.max_nodes,
                   "max_edges": lim.max_edges},
        "truncation": truncation,
        "issues": sorted(set(sc.issues)),
        "entity_count": entities,
        "duplicate_ids": duplicates,
        "counts_by_type": dict(sorted(counts.items())),
        "string_fields_by_class": dict(sorted(by_class.items())),
        "classification_note": "heuristic candidates only; no precision/recall claimed",
        "nodes": nodes,
        "edges": edges,
        "dangling_refs": dangling[:1000],
        "dangling_ref_count": len(dangling),
    }
