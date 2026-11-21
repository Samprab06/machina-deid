"""Attempt 2c: frozen shared entity map for cross-document PDF resolution.

Pass 1 (``EntityMapBuilder``) scans PDFs with :mod:`pdf_titleblock`, votes a kind per normalized value,
marks ambiguous values, merges only conservative *corroborated* aliases, and freezes a SQLite map keyed by
HMAC digests (no raw values, no secret). Pass 2 (``Resolver``) answers "is this exact text a known identity,
and what is its pseudonym" for any PDF, so a person found in the title block of drawing A is also replaced
where it appears unlabeled in drawing B.

Pseudonyms: a value already approved in the identity catalog keeps the catalog pseudonym (so PDF, STEP and
DXF agree). Otherwise :class:`identity.PseudonymMapper` derives the deterministic, secret-keyed pseudonym of
the alias root. Each pseudonym also has a *fitted* short form with the same token (unique per kind code),
used only when the full form cannot fit the original text extent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .discovery import normalize
from .identity import PseudonymMapper, _normalize

FORMAT = "machina-deid/pdf-entity-map/1"
# kind -> two-letter code of the fitted form (distinct even where full labels share a first letter)
FIT_CODES = {"person": "PE", "org": "OR", "project": "PJ", "part": "PT", "address": "AD", "account": "AC",
             "text": "ID", "phone": "PH", "email": "EM", "domain": "DO"}
_ORG_SUFFIX = re.compile(r"[\s,.]+(?:ltd|limited|inc|incorporated|llc|l\.l\.c|gmbh|ag|co|company|corp|corporation|"
                         r"plc|pty|bv|b\.v|sa|s\.a|srl|s\.r\.l|oy|ab|as|kg|nv|llp|lp)\.?$", re.I)
_PATTERN_KINDS = ("email", "phone", "domain")


class EntityMapError(Exception):
    """Configuration/consistency error; never contains raw values."""


def fitted(pseudonym: str, kind: str) -> str:
    """Short form keeping the unique token: ``Person-1A2B3`` -> ``PE-1A2B3``; email/domain stay valid shapes."""
    if kind == "email":
        local = pseudonym.split("@", 1)[0].replace("user-", "")
        return f"{local}@ex.invalid"
    if kind == "domain":
        return pseudonym.replace("domain-", "d-").replace(".example.invalid", ".invalid")
    if kind == "phone":
        return pseudonym
    token = pseudonym.rsplit("-", 1)[-1]
    return f"{FIT_CODES.get(kind, 'ID')}-{token}"


def org_stem(value: str) -> str:
    v = normalize(value)
    prev = None
    while prev != v:
        prev, v = v, _ORG_SUFFIX.sub("", v).strip(" ,.")
    return re.sub(r"[^\w]+", " ", v).strip()


def person_parts(value: str) -> tuple[str, str] | None:
    """(first initial, surname) of a person-shaped value, or None."""
    toks = [t for t in re.split(r"[\s.]+", normalize(value)) if t]
    if len(toks) < 2:
        return None
    if "," in value:  # "Smith, John"
        sur, first = normalize(value).split(",", 1)
        first = first.strip()
        return (first[:1], sur.strip()) if first and sur.strip() else None
    return toks[0][:1], toks[-1]


def is_initial_form(value: str) -> bool:
    toks = [t for t in re.split(r"[\s.]+", value.strip()) if t]
    return len(toks) >= 2 and all(len(t) == 1 for t in toks[:-1]) and len(toks[-1]) > 1


@dataclass
class _Obs:
    kinds: Counter = field(default_factory=Counter)
    docs: set = field(default_factory=set)
    families: set = field(default_factory=set)
    labeled: int = 0
    display: str = ""
    ole: set[str] = field(default_factory=set)  # exact values of parsed OLE saved-account properties


class EntityMapBuilder:
    """Collect findings across documents (raw values only in memory), then ``freeze`` to disk."""

    def __init__(self, secret: bytes, catalog=None, *, ole_alias_key: bytes | None = None):
        if len(secret) < 16:
            raise EntityMapError("secret too short")
        self._secret = bytes(secret)
        self._catalog = catalog
        self._ole_key = ole_alias_key
        self._obs: dict[str, _Obs] = defaultdict(_Obs)  # normalized value -> observations
        self.documents = 0

    def add_document(self, rel: str, findings) -> None:
        self.documents += 1
        fam = family_of(rel)
        for f in findings:
            if f.source == "lookup":
                continue
            n = _normalize(f.value)
            if not n:
                continue
            o = self._obs[n]
            o.docs.add(rel)
            o.families.add(fam)
            o.labeled += f.source.startswith(("label", "structured")) or f.source in ("inline", "inline_row")
            if f.source.startswith("structured:ole"):
                o.ole.add(f.value)
                continue
            o.kinds[f.kind] += 1
            o.display = o.display or f.value

    # -------------------------------------------------------------- decisions

    def _decide(self) -> tuple[dict[str, dict], Counter]:
        stats: Counter = Counter()
        decided: dict[str, dict] = {}
        for n, o in self._obs.items():
            if not o.kinds:  # seen only as an OLE saved account
                o.kinds["account"] += 1
                o.display = min(o.ole)
            (kind, top), *rest = o.kinds.most_common() + [("", 0)]
            second = rest[0][1] if rest else 0
            amb = kind not in _PATTERN_KINDS and second > 0 and top < 4 * second
            decided[n] = {"kind": kind, "status": "ambiguous" if amb else "ok", "obs": o}
            stats["ambiguous" if amb else f"kind:{kind}"] += 1
        # conservative aliases -> root normalized value
        root: dict[str, str] = {}
        by_stem: dict[str, list[str]] = defaultdict(list)
        by_person: dict[tuple[str, str], list[str]] = defaultdict(list)
        for n, d in decided.items():
            if d["status"] != "ok":
                continue
            if d["kind"] == "org":
                by_stem[org_stem(n)].append(n)
            elif d["kind"] == "person" and not is_initial_form(d["obs"].display):
                pp = person_parts(d["obs"].display)
                if pp:
                    by_person[pp].append(n)
        for stem, group in by_stem.items():
            if len(group) > 1 and stem and len(stem) >= 3:
                r = sorted(group, key=lambda g: (-len(decided[g]["obs"].docs), g))[0]
                for g in group:
                    if g != r:
                        root[g] = r
                        stats["alias:org_suffix"] += 1
        for n, d in decided.items():
            if d["status"] != "ok" or d["kind"] != "person" or not is_initial_form(d["obs"].display):
                continue
            pp = person_parts(d["obs"].display)
            fulls = by_person.get(pp, []) if pp else []
            if len(fulls) == 1 and (decided[fulls[0]]["obs"].families & d["obs"].families):
                root[n] = fulls[0]  # unique full name + same document family: corroborated
                stats["alias:initial_surname"] += 1
            elif len(fulls) > 1:
                stats["alias_ambiguous:initial_surname"] += 1
        return {n: {**d, "root": root.get(n, n)} for n, d in decided.items()}, stats

    def freeze(self, path: str | Path) -> dict:
        path = Path(path)
        if path.exists():
            raise EntityMapError("entity map exists; frozen maps are never overwritten")
        decided, stats = self._decide()
        mapper = PseudonymMapper(self._secret)
        from .ole_transform import OleError, equal_length_alias, widths_match

        members: dict[str, list[str]] = defaultdict(list)
        for value, decision in sorted(decided.items()):
            members[decision["root"]].append(value)

        # Canonical and fitted tokens are both visible in outputs. Reserve each
        # token across every root before allowing a row to resolve.
        aliases = {}
        token_roots: dict[str, set[str]] = defaultdict(set)
        conflicted = set()
        for root, group in sorted(members.items()):
            kind = decided[root]["kind"]
            approved = set()
            if self._catalog is not None:
                for value in group:
                    hit = self._catalog.approved_pseudonym(value)
                    if hit is not None:
                        approved.add(hit[1])
            if len(approved) > 1:
                conflicted.add(root)
                stats["alias_conflicting_catalog_values"] += 1
            pseudonym = min(approved) if approved else None
            if pseudonym is not None:
                stats["catalog_pseudonym"] += 1

            accounts = sorted({account for value in group for account in decided[value]["obs"].ole})
            if pseudonym is None and accounts and self._ole_key is not None:
                try:
                    candidate = equal_length_alias(accounts[0], self._ole_key)
                    if all(candidate != value and widths_match(value, candidate) for value in accounts):
                        pseudonym = candidate
                        stats["ole_equal_length_pseudonym"] += 1
                except OleError:
                    stats["ole_no_width_alias"] += 1
            if pseudonym is None:
                pseudonym = mapper.pseudonym(kind if kind in FIT_CODES else "text", root)
            fitting = pseudonym if accounts and all(widths_match(value, pseudonym) for value in accounts) else fitted(pseudonym, kind)
            aliases[root] = (kind, pseudonym, fitting)
            if decided[root]["status"] == "ok":
                for token in (pseudonym, fitting):
                    token_roots[_normalize(token)].add(root)

        for roots in token_roots.values():
            if len(roots) > 1:
                conflicted.update(roots)
                stats["pseudonym_shared_across_roots"] += 1

        rows = []
        for value, decision in sorted(decided.items()):
            root = decision["root"]
            kind, pseudonym, fitting = aliases[root]
            status = "ambiguous" if root in conflicted else decision["status"]
            observation = decision["obs"]
            lookup_ok = int(status == "ok" and _lookup_eligible(kind, observation))
            rows.append((_vkey(self._secret, value), kind, pseudonym, fitting, status, len(observation.docs),
                         int(observation.labeled), lookup_ok, _vkey(self._secret, root)))
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        db = sqlite3.connect(tmp)
        try:
            db.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE entities (value_key TEXT PRIMARY KEY, kind TEXT NOT NULL, pseudonym TEXT NOT NULL,
                    fitted TEXT NOT NULL, status TEXT NOT NULL, docs INTEGER NOT NULL, labeled INTEGER NOT NULL,
                    lookup_ok INTEGER NOT NULL, root_key TEXT NOT NULL);""")
            db.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,?)", rows)
            digest = _rows_digest(rows)
            db.executemany("INSERT INTO meta VALUES (?,?)", [
                ("format", FORMAT), ("fingerprint", _fingerprint(self._secret)), ("digest", digest),
                ("documents", str(self.documents)), ("stats", json.dumps(dict(sorted(stats.items()))))])
            db.commit()
        finally:
            db.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return {"entities": len(rows), "documents": self.documents, "digest": digest, **dict(stats)}


def _lookup_eligible(kind: str, o: _Obs) -> bool:
    """May this value be replaced where it appears *without* a role label? Multi-token names and pattern
    kinds yes; single-token values only when labeled in >= 2 documents (corroborated)."""
    if kind in _PATTERN_KINDS:
        return True
    toks = o.display.split()
    if len(toks) >= 2 and len(normalize(o.display)) >= 5:
        return True
    return len(o.docs) >= 2 and o.labeled >= 2 and len(normalize(o.display)) >= 4


def _vkey(secret: bytes, normalized: str) -> str:
    return hmac.new(secret, b"machina-deid/entity/value\x00" + normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def _fingerprint(secret: bytes) -> str:
    return hmac.new(secret, b"machina-deid/fingerprint", hashlib.sha256).hexdigest()[:16]


def _rows_digest(rows) -> str:
    h = hashlib.sha256()
    for r in sorted(rows):
        h.update(json.dumps(r).encode())
    return h.hexdigest()


def family_of(rel: str) -> str:
    """Document family: parent directory (manifest-relative). Used for alias corroboration and holdouts."""
    p = PurePosixPath(rel.replace("\\", "/"))
    return str(p.parent) if str(p.parent) not in ("", ".") else p.stem.split("_")[0].split("-")[0].lower()


@dataclass
class Entry:
    kind: str
    pseudonym: str
    fitted: str
    status: str
    lookup_ok: bool


class Resolver:
    """Pass-2 resolution: frozen entity map (optional) + this file's catalog-approved strings (optional).

    ``derive=True`` (local sample runs without a frozen map) derives deterministic pseudonyms for labeled
    findings directly; production uses a frozen map and leaves unknown values unresolved (reported)."""

    def __init__(self, secret: bytes | None = None, map_path: str | Path | None = None, *,
                 catalog_strings: dict[str, str] | None = None, derive: bool = False, expect_digest: str = ""):
        self._secret = bytes(secret) if secret else None
        self._db = None
        self.digest = ""
        if map_path is not None:
            if self._secret is None:
                raise EntityMapError("entity map requires the secret")
            self._db = sqlite3.connect(Path(map_path).resolve().as_uri() + "?mode=ro", uri=True)
            meta = dict(self._db.execute("SELECT key, value FROM meta"))
            if meta.get("format") != FORMAT or meta.get("fingerprint") != _fingerprint(self._secret):
                raise EntityMapError("entity map format/secret mismatch")
            self.digest = meta["digest"]
            if expect_digest and expect_digest != self.digest:
                raise EntityMapError("entity map digest differs from the expected frozen digest")
        self._catalog = {_normalize(k): v for k, v in (catalog_strings or {}).items()}
        self._derive = derive and self._secret is not None
        self._mapper = PseudonymMapper(self._secret) if self._derive else None
        self._cache: dict[str, Entry | None] = {}
        self.local: dict[str, Entry] = {}  # values resolved in the current document (duplicates elsewhere)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()

    def _entry(self, value: str) -> Entry | None:
        n = _normalize(value)
        if not n:
            return None
        if n in self._cache:
            return self._cache[n]
        e = None
        if n in self._catalog:
            ps = self._catalog[n]
            e = Entry("text", ps, ps, "ok", True)
        if self._db is not None:
            row = self._db.execute("SELECT kind, pseudonym, fitted, status, lookup_ok FROM entities WHERE value_key=?",
                                   (_vkey(self._secret, n),)).fetchone()
            if row is not None:
                if e is not None:  # catalog pseudonym wins; keep the map's kind for the fitted form
                    e = Entry(row[0], e.pseudonym, fitted(e.pseudonym, row[0]), "ok", True)
                else:
                    e = Entry(row[0], row[1], row[2], row[3], bool(row[4]))
        self._cache[n] = e
        return e

    def resolve(self, kind: str, value: str) -> tuple[Entry | None, str]:
        """(entry, reason). reason is '' on success, else a stable failure code."""
        e = self._entry(value)
        if e is not None:
            if e.status != "ok":
                return None, "ambiguous_identity"
            self.local[_normalize(value)] = e
            return e, ""
        if self._derive:
            k = kind if kind in FIT_CODES else "text"
            ps = self._mapper.pseudonym(k, value)
            e = Entry(k, ps, fitted(ps, k), "ok", False)
            self.local[_normalize(value)] = e
            return e, ""
        return None, "not_in_frozen_map"

    def lookup(self, value: str):
        """For unlabeled n-gram spans: (kind, pseudonym) if eligible, else None."""
        n = _normalize(value)
        e = self.local.get(n)
        if e is None:
            e = self._entry(value)
            if e is None or e.status != "ok" or not e.lookup_ok:
                return None
        return e.kind, e.pseudonym

    def entry(self, value: str) -> Entry | None:
        return self.local.get(_normalize(value)) or self._entry(value)

    def begin_document(self) -> None:
        self.local = {}
