"""Phase 5D: one narrowly scoped STEP DATA transformation -- ``ORGANIZATION.name``.

Schema semantics (ISO 10303-41, ``application_context_schema`` / ``person_organization_schema``)::

    ENTITY organization;
      id          : OPTIONAL identifier;   -- parameter 1 (index 0)
      name        : label;                 -- parameter 2 (index 1)  <- the only value ever replaced
      description : OPTIONAL text;         -- parameter 3 (index 2)
    END_ENTITY;

Organization *roles* are not attributes of ``ORGANIZATION``; they live in ``ORGANIZATION_ROLE`` and the
``*_ORGANIZATION_ASSIGNMENT`` entities, which this module never touches. ``label``, ``identifier`` and
``text`` are all ``STRING``.

Scope: only simple (non-complex) instances whose single entity type is exactly ``ORGANIZATION``, with exactly
three top-level parameters, whose second parameter is a plain string literal (not ``$``, not a typed
value, not nested). That literal is replaced only when its decoded value is *exactly* (character for
character) a key of the explicit ``org_map``. ``org_map`` is built by the caller from approvals recorded
under the typed ``org`` kind. Nothing is regex-replaced across DATA. Every other byte, including entity ids,
references, the ``id``/``description`` strings, numbers, units, comments, whitespace and line endings,
is copied unchanged. Only the edited string literals are spliced.

Verification of the output: it is reparsed with the same structural scanner and the following must hold:
the entity id sequence, entity types, top-level arities and the ordered reference lists (by parameter path)
are identical; the string-literal count per entity is identical; the numeric-token sequence outside string
literals is identical; the file with the edited literals cut out is byte-identical to the input with the
original literals cut out; each edited literal decodes to the planned pseudonym; no mapped original remains
at an ``ORGANIZATION.name`` position.

Limitation: **no geometry/topology validation is performed**, because no trusted geometry kernel is
available here. B-rep validity is argued only from the structural invariants above. Geometric entities
are never edited and all numeric tokens are proven unchanged, but nothing was checked with a kernel.

Outcomes: ``transformed_validated`` | ``inspected_no_identifiers`` | ``identifiers_unmodifiable`` |
``failed_validation``. Results never contain raw values.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import step_analysis as sa
from . import step_transform as st

ENTITY = "ORGANIZATION"
NAME_INDEX = 1  # 0-based top-level parameter index of ``name``
ARITY = 3
MAX_BYTES = 64 * 1024 * 1024
GEOMETRY_VALIDATION = "not_performed: no trusted geometry kernel available; structural/numeric invariants only"
_NUM = re.compile(r"(?<![A-Za-z0-9_#.])[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[Ee][+-]?\d+)?")


class StepDataError(Exception):
    """Short reason code only, never content."""


@dataclass
class OrgSite:
    eid: int
    start: int  # opening quote offset
    end: int  # just past closing quote
    value: str | None  # decoded exact value; None if undecodable
    etype: str = ENTITY
    pidx: int = NAME_INDEX


@dataclass
class Parsed:
    text: str
    records: list  # sa._Record
    data_start: int
    sites: list[OrgSite]


def decode_literal(raw: str) -> str | None:
    """Decode the content between the quotes of a Part 21 string (latin-1 view). ``None`` if a
    directive cannot be decoded or 8-bit bytes are not valid UTF-8."""
    if not raw.isascii():  # raw 8-bit bytes (non-conforming but common): accept only valid UTF-8
        try:
            raw = raw.encode("latin-1").decode("utf-8")
        except UnicodeDecodeError:
            return None
    text, directives = st._split_content(raw)
    if directives:
        if "\\\\" in raw:  # escaped backslash mixed with directives is ambiguous for the decoder
            return None
        text = st._decode_directives(text)
        if "\\" in text:  # an unsupported or malformed directive was left undecoded
            return None
    return text


# Attempt 2: identity-bearing DATA attribute positions (ISO 10303-41 person_organization_schema).
# type -> (arity, string-parameter indices). Only exact decoded equality with an approved value is replaced.
ORG_SPEC = {ENTITY: (ARITY, (NAME_INDEX,))}
IDENTITY_SPEC = {
    "ORGANIZATION": (3, (0, 1)),          # id, name
    "PERSON": (6, (0, 1, 2)),             # id, last_name, first_name
}


def parse(data: bytes, spec: dict = ORG_SPEC) -> Parsed:
    if len(data) > MAX_BYTES:
        raise StepDataError("file_too_large")
    text = data.decode("latin-1")
    sc = sa._Scanner(text, sa.AnalysisLimits(max_bytes=MAX_BYTES, max_string_len=1 << 30))
    try:
        if not sc.find_data_section():
            raise StepDataError("no_data_section")
        data_start = sc.i
        recs = []
        while True:
            r = sc.next_record()
            if r is None:
                break
            if r == "skip":
                raise StepDataError("malformed_record")
            recs.append(r)
    except sa.StepAnalysisError as exc:
        raise StepDataError("parse_error") from exc
    if sc.issues:
        raise StepDataError("parse_issues")
    if len({r.eid for r in recs}) != len(recs):
        raise StepDataError("duplicate_entity_ids")
    sites = []
    for r in recs:
        if len(r.types) != 1 or r.types[0] not in spec or r.arity != [spec[r.types[0]][0]]:
            continue
        idxs = {str(i): i for i in spec[r.types[0]][1]}
        for (path, _, _, _), (a, b) in zip(r.strings, r.spans):
            if path in idxs:
                sites.append(OrgSite(r.eid, a, b, decode_literal(text[a + 1:b - 1]), r.types[0], idxs[path]))
    return Parsed(text, recs, data_start, sites)


def organization_names(path: str | Path) -> list[tuple[int, str]]:
    """``(entity id, exact decoded name)`` for every in-scope ORGANIZATION. Used by discovery."""
    p = parse(Path(path).read_bytes())
    return [(s.eid, s.value) for s in p.sites if s.value is not None]


# ---------------------------------------------------------------- invariants


def _skeleton(text: str, recs) -> tuple[str, list[str]]:
    """DATA text with every string literal cut out, plus numeric tokens outside literals/comments."""
    parts, pos = [], recs[0].start if recs else len(text)
    for r in recs:
        for a, b in r.spans:
            parts.append(text[pos:a])
            pos = b
    parts.append(text[pos:])
    sk = "\x00".join(parts)
    nums = _NUM.findall(re.sub(r"/\*.*?\*/", " ", sk, flags=re.S))
    return sk, nums


def graph(recs) -> list[tuple]:
    return [(r.eid, tuple(r.types), tuple(r.arity), tuple(r.refs), len(r.spans)) for r in recs]


# ---------------------------------------------------------------- result


@dataclass
class StepDataResult:
    outcome: str = "failed_validation"
    ok: bool = False
    reason: str = ""
    output_written: bool = False
    source_sha256: str = ""
    output_sha256: str = ""
    entities: int = 0
    references: int = 0
    organizations_in_scope: int = 0
    replacements: dict[int, int] = field(default_factory=dict)  # org_map index -> count
    unmodifiable: int = 0  # in-scope names whose value could not be decoded
    graph_identical: bool = False
    numeric_tokens: int = 0
    numeric_tokens_identical: bool = False
    non_target_bytes_identical: bool = False
    geometry_validation: str = GEOMETRY_VALIDATION

    def to_dict(self) -> dict:
        return asdict(self)


def _fail(res: StepDataResult, reason: str) -> StepDataResult:
    res.outcome, res.ok, res.reason, res.output_written = "failed_validation", False, reason, False
    return res


def _split_pseudonym(new: str) -> tuple[str, str]:
    """Pseudonym -> (first, last) for PERSON first_name/last_name pairs."""
    head, _, tail = new.partition(" ")
    return (head, tail) if tail else ("", new)


def plan(p: Parsed, org_map: dict[str, str]) -> tuple[list[tuple[OrgSite, str, int]], int]:
    index = {k: i for i, k in enumerate(org_map)}
    edits, bad = [], 0
    done: set[int] = set()
    # PERSON split names: last/first jointly equal to an approved full name (exact, whitespace-normalised join)
    by_eid: dict[int, dict[int, OrgSite]] = {}
    for s in p.sites:
        if s.etype == "PERSON":
            by_eid.setdefault(s.eid, {})[s.pidx] = s
    for eid, ps in by_eid.items():
        last, first = ps.get(1), ps.get(2)
        if not last or not first or not last.value or not first.value:
            continue
        if last.value in org_map or first.value in org_map:
            continue  # handled individually below
        for joined in (f"{first.value} {last.value}", f"{last.value}, {first.value}", f"{last.value} {first.value}"):
            if joined in org_map:
                f_new, l_new = _split_pseudonym(org_map[joined])
                edits.append((first, f_new, index[joined]))
                edits.append((last, l_new, index[joined]))
                done.update((first.start, last.start))
                break
    for s in p.sites:
        if s.start in done:
            continue
        if s.value is None:
            bad += 1
            continue
        if s.value in org_map:  # exact decoded string equality only
            edits.append((s, org_map[s.value], index[s.value]))
    return edits, bad


def transform_identities(src: str | Path, dst: str | Path, id_map: dict[str, str], *,
                         overwrite: bool = False) -> StepDataResult:
    """Attempt 2: replace exact approved values at every :data:`IDENTITY_SPEC` position."""
    safe = {k: v for k, v in id_map.items() if isinstance(k, str) and k and k != v and st._safe_new(v)}
    return transform_organizations(src, dst, safe, overwrite=overwrite, spec=IDENTITY_SPEC)


def identity_values(path: str | Path) -> list[tuple[str, str]]:
    """``(location, exact value)`` for every IDENTITY_SPEC site plus joined PERSON names (discovery)."""
    p = parse(Path(path).read_bytes(), IDENTITY_SPEC)
    out, persons = [], {}
    for s in p.sites:
        if s.value is None:
            continue
        out.append((f"{s.etype}.{s.pidx}", s.value))
        if s.etype == "PERSON":
            persons.setdefault(s.eid, {})[s.pidx] = s.value
    for ps in persons.values():
        if ps.get(1) and ps.get(2):
            out.append(("PERSON.full", f"{ps[2]} {ps[1]}"))
            out.append(("PERSON.full", f"{ps[1]}, {ps[2]}"))
            out.append(("PERSON.full", f"{ps[1]} {ps[2]}"))
    return out


def transform_organizations(src: str | Path, dst: str | Path, org_map: dict[str, str], *,
                            overwrite: bool = False, spec: dict = ORG_SPEC) -> StepDataResult:
    """Replace ``ORGANIZATION.name`` literals whose exact decoded value is in ``org_map``.

    ``dst`` is written atomically, and only for ``transformed_validated``."""
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("output path must differ from source path")
    if dst.exists() and not overwrite:
        raise FileExistsError("output exists")
    for k, v in org_map.items():
        if not isinstance(k, str) or not k or not st._safe_new(v) or k == v:
            raise ValueError("org_map entries must be non-empty, distinct, printable-ASCII replacements")
    res = StepDataResult()
    try:
        data = src.read_bytes()
    except OSError:
        return _fail(res, "io_error")
    res.source_sha256 = hashlib.sha256(data).hexdigest()
    try:
        p = parse(data, spec)
    except StepDataError as exc:
        return _fail(res, str(exc))
    res.entities = len(p.records)
    res.references = sum(len(r.refs) for r in p.records)
    res.organizations_in_scope = len(p.sites)
    edits, res.unmodifiable = plan(p, org_map)
    if not edits:
        if res.unmodifiable:
            res.outcome, res.reason = "identifiers_unmodifiable", "undecodable_organization_name"
        else:
            res.outcome, res.ok, res.reason = "inspected_no_identifiers", True, "no_exact_organization_match"
        return res

    pieces, pos = [], 0
    for s, new, _ in sorted(edits, key=lambda e: e[0].start):
        pieces.append(p.text[pos:s.start])
        pieces.append("'" + st._escape(new) + "'")
        pos = s.end
    pieces.append(p.text[pos:])
    out = "".join(pieces).encode("latin-1")

    reason = _verify(p, out, edits, org_map, res, spec)
    if reason:
        return _fail(res, reason)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:10]}.partial")
    try:
        with tmp.open("wb") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        if tmp.read_bytes() != out:
            return _fail(res, "output_readback_mismatch")
        if hashlib.sha256(src.read_bytes()).hexdigest() != res.source_sha256:
            return _fail(res, "source_changed_during_transform")
        if dst.exists() and not overwrite:
            return _fail(res, "output_exists")
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    res.outcome, res.ok, res.reason, res.output_written = "transformed_validated", True, "", True
    res.output_sha256 = hashlib.sha256(out).hexdigest()
    for _, _, i in edits:
        res.replacements[i] = res.replacements.get(i, 0) + 1
    return res


def _verify(p: Parsed, out: bytes, edits, org_map: dict[str, str], res: StepDataResult, spec: dict = ORG_SPEC) -> str:
    try:
        q = parse(out, spec)
    except StepDataError:
        return "output_not_parseable"
    if q.text[:q.data_start] != p.text[:p.data_start]:
        return "header_changed"
    if graph(p.records) != graph(q.records):
        return "entity_graph_changed"
    res.graph_identical = True
    sk_in, num_in = _skeleton(p.text, p.records)
    sk_out, num_out = _skeleton(q.text, q.records)
    if num_in != num_out:
        return "numeric_tokens_changed"
    res.numeric_tokens, res.numeric_tokens_identical = len(num_in), True
    if sk_in != sk_out:
        return "non_string_bytes_changed"
    # every literal other than the planned ones is byte-identical
    planned = {s.start for s, _, _ in edits}
    old_lits = [(a, p.text[a:b]) for r in p.records for a, b in r.spans]
    new_lits = [q.text[a:b] for r in q.records for a, b in r.spans]
    if len(old_lits) != len(new_lits):
        return "string_count_changed"
    want = {s.start: new for s, new, _ in edits}
    for (a, old), new in zip(old_lits, new_lits):
        if a in planned:
            if decode_literal(new[1:-1]) != want[a] or new != "'" + st._escape(want[a]) + "'":
                return "replacement_not_as_planned"
        elif old != new:
            return "unplanned_string_changed"
    res.non_target_bytes_identical = True
    if len(q.sites) != len(p.sites):
        return "organization_count_changed"
    reintroduced = {k for k in org_map if k in org_map.values()}
    if any(s.value in org_map and s.value not in reintroduced for s in q.sites):
        return "mapped_original_remains"
    return ""
