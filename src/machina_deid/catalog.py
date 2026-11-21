"""Persistent global identity catalog (Phase 5A).

A SQLite catalog that complements :mod:`identity` and :mod:`discovery`. It does not change any
transformer behavior; nothing reads it yet except callers that opt in.

Guarantees:

* **No raw identities persisted.** Values are reduced to keyed HMAC-SHA256 digests using the
  existing secret and the exact :func:`identity._normalize` semantics (NFKC, casefold, strip,
  collapse whitespace). Source paths are stored only as keyed digests. Reports, ``repr`` and
  exception messages never contain raw values.
* **Secret binding.** The secret fingerprint (same as :class:`PseudonymMapper`) is stored; opening
  with a different secret raises :class:`CatalogError`.
* **Deterministic pseudonyms.** Confirmed ``(kind, identity)`` pairs map through a
  :class:`PseudonymMapper` backed by a protected (0600, atomically written) JSON store.
* **No fuzzy merging.** Two identities are linked only by :meth:`IdentityCatalog.assert_alias`,
  an explicit caller assertion between two exact identities of the same kind. Contradictory
  assertions are rejected and recorded as conflicts.
* **Atomic + idempotent.** Each public mutating call is a single transaction (all or nothing).
  Observations have a deterministic key, so re-inserting after a restart is a no-op.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .discovery import CONFIDENCES
from .identity import KINDS, IdentityError, PseudonymMapper, _normalize

SCHEMA_VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TEXT = re.compile(r"^[\x20-\x7e]{1,200}$")


class CatalogError(Exception):
    """Catalog configuration/consistency error. Never contains raw identities or secrets."""


_MIGRATIONS: dict[int, list[str]] = {
    1: [
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        # Confirmed exact identities. id_key = HMAC(kind \0 normalized).
        """CREATE TABLE identities (
            id_key TEXT PRIMARY KEY, kind TEXT NOT NULL, value_key TEXT NOT NULL,
            pseudonym TEXT NOT NULL UNIQUE, alias_of TEXT REFERENCES identities(id_key))""",
        "CREATE INDEX identities_value ON identities(value_key)",
        # Every observation (confirmed or candidate). value_key = HMAC("value" \0 normalized).
        """CREATE TABLE observations (
            obs_key TEXT PRIMARY KEY, value_key TEXT NOT NULL, kind TEXT NOT NULL,
            id_key TEXT REFERENCES identities(id_key), source_sha256 TEXT NOT NULL,
            path_id TEXT NOT NULL, location TEXT NOT NULL, confidence TEXT NOT NULL,
            reason TEXT NOT NULL)""",
        "CREATE INDEX observations_value ON observations(value_key)",
        """CREATE TABLE conflicts (
            conflict_key TEXT PRIMARY KEY, type TEXT NOT NULL, subject TEXT NOT NULL,
            other TEXT NOT NULL, detail TEXT NOT NULL)""",
    ],
}


@dataclass(frozen=True)
class Observation:
    """One sighting of a value in a source file. ``value`` is never repr'd or persisted."""

    kind: str
    value: str
    source_sha256: str
    path: str
    location: str
    confidence: str
    reason: str

    def __repr__(self) -> str:
        return f"Observation(kind={self.kind!r}, location={self.location!r}, confidence={self.confidence!r})"


class IdentityCatalog:
    def __init__(self, db_path: str | os.PathLike, secret: bytes, store_path: str | os.PathLike | None = None):
        db_path = Path(db_path)
        store_path = Path(store_path) if store_path is not None else db_path.with_suffix(".pseudonyms.json")
        try:
            self._mapper = PseudonymMapper(secret, store_path)
        except IdentityError as exc:
            raise CatalogError(str(exc)) from None
        self._secret = bytes(secret)
        self._fingerprint = self._mapper._fingerprint()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        new = not db_path.exists()
        self._db = sqlite3.connect(db_path, isolation_level=None)
        if new:
            os.chmod(db_path, 0o600)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        self._migrate()
        self._check_secret()
        self._check_store_consistency()

    def __repr__(self) -> str:
        return "IdentityCatalog(<redacted>)"

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "IdentityCatalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _migrate(self) -> None:
        current = self._db.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise CatalogError("catalog schema is newer than this software")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            with self._tx() as db:
                for stmt in _MIGRATIONS[version]:
                    db.execute(stmt)
                if version == 1:
                    db.execute("INSERT INTO meta VALUES ('secret_fingerprint', ?)", (self._fingerprint,))
                db.execute(f"PRAGMA user_version = {version}")

    def _check_secret(self) -> None:
        row = self._db.execute("SELECT value FROM meta WHERE key='secret_fingerprint'").fetchone()
        if row is None or not hmac.compare_digest(row[0], self._fingerprint):
            self.close()
            raise CatalogError("catalog was created with a different secret")

    def _check_store_consistency(self) -> None:
        # Every catalog pseudonym must exist in the protected store (e.g. store deleted/replaced).
        known = set(self._mapper._map.values())
        missing = sum(1 for (p,) in self._db.execute("SELECT pseudonym FROM identities") if p not in known)
        if missing:
            self.close()
            raise CatalogError(f"pseudonym store is missing {missing} catalog entries")

    # ------------------------------------------------------------ keys

    def _hmac(self, *parts: str) -> str:
        msg = b"\x00".join(p.encode("utf-8") for p in parts)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def identity_key(self, kind: str, value: str) -> str:
        """Same digest as PseudonymMapper (kind \\0 normalized)."""
        self._check_kind(kind)
        return self._mapper._digest(kind, self._norm(value)).hex()

    def value_key(self, value: str) -> str:
        return self._hmac("machina-deid/catalog/value", self._norm(value))

    def path_id(self, path: str) -> str:
        return self._hmac("machina-deid/catalog/path", path.replace("\\", "/"))[:32]

    @staticmethod
    def _norm(value: str) -> str:
        if not isinstance(value, str) or not _normalize(value):
            raise CatalogError("identity must be a non-empty string")
        return _normalize(value)

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in KINDS:
            raise CatalogError(f"unknown kind (allowed: {', '.join(sorted(KINDS))})")

    # ------------------------------------------------------------ identities

    def _ensure_identity(self, db: sqlite3.Connection, kind: str, value: str) -> tuple[str, str]:
        id_key = self.identity_key(kind, value)
        row = db.execute("SELECT pseudonym FROM identities WHERE id_key=?", (id_key,)).fetchone()
        if row:
            return id_key, row[0]
        pseudonym = self._mapper.pseudonym(kind, value)  # persisted atomically; deterministic
        db.execute("INSERT INTO identities (id_key, kind, value_key, pseudonym) VALUES (?,?,?,?)",
                   (id_key, kind, self.value_key(value), pseudonym))
        return id_key, pseudonym

    def _root(self, db: sqlite3.Connection, id_key: str) -> str:
        seen = set()
        while True:
            row = db.execute("SELECT alias_of FROM identities WHERE id_key=?", (id_key,)).fetchone()
            if row is None or row[0] is None:
                return id_key
            if id_key in seen:  # pragma: no cover - prevented on insert
                raise CatalogError("alias cycle detected")
            seen.add(id_key)
            id_key = row[0]

    def pseudonym(self, kind: str, value: str) -> str:
        """Register (if needed) and return the pseudonym for an exact identity, following explicit aliases."""
        with self._tx() as db:
            id_key, _ = self._ensure_identity(db, kind, value)
            root = self._root(db, id_key)
            return db.execute("SELECT pseudonym FROM identities WHERE id_key=?", (root,)).fetchone()[0]

    def lookup(self, kind: str, value: str) -> str | None:
        """Pseudonym if the identity is already confirmed, else None. Never registers."""
        id_key = self.identity_key(kind, value)
        if not self._db.execute("SELECT 1 FROM identities WHERE id_key=?", (id_key,)).fetchone():
            return None
        root = self._root(self._db, id_key)
        return self._db.execute("SELECT pseudonym FROM identities WHERE id_key=?", (root,)).fetchone()[0]

    # ------------------------------------------------------------ observations

    def _validate(self, o: Observation, confirmed: bool) -> None:
        if confirmed:
            self._check_kind(o.kind)
        elif not isinstance(o.kind, str) or not _SAFE_TEXT.fullmatch(o.kind):
            raise CatalogError("candidate kind must be short printable ASCII")
        self._norm(o.value)
        if not isinstance(o.source_sha256, str) or not _HEX64.match(o.source_sha256):
            raise CatalogError("source_sha256 must be 64 lowercase hex chars")
        if not isinstance(o.path, str) or not o.path:
            raise CatalogError("path must be a non-empty string")
        if o.confidence not in CONFIDENCES:
            raise CatalogError(f"confidence must be one of {CONFIDENCES}")
        norm = _normalize(o.value)
        for name in ("location", "reason"):
            text = getattr(o, name)
            if not isinstance(text, str) or not _SAFE_TEXT.fullmatch(text):
                raise CatalogError(f"{name} must be short printable ASCII")
            if len(norm) >= 3 and norm in _normalize(text):
                raise CatalogError(f"{name} must not contain the observed value")

    def _insert(self, db: sqlite3.Connection, o: Observation, confirmed: bool) -> bool:
        self._validate(o, confirmed)
        id_key = self._ensure_identity(db, o.kind, o.value)[0] if confirmed else None
        vkey = self.value_key(o.value)
        pid = self.path_id(o.path)
        obs_key = self._hmac("machina-deid/catalog/obs", "c" if confirmed else "u", o.kind, vkey,
                             o.source_sha256, pid, o.location)
        cur = db.execute(
            "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
            (obs_key, vkey, o.kind, id_key, o.source_sha256, pid, o.location, o.confidence, o.reason))
        return cur.rowcount == 1

    def record(self, observations: Iterable[Observation], *, confirmed: bool) -> int:
        """Insert observations in one transaction; returns how many were new (duplicates ignored).

        ``confirmed=True``: kind must be an identity kind; the identity gets a pseudonym.
        ``confirmed=False``: a discovery candidate (any discovery kind); no pseudonym is allocated.
        Any invalid item rolls back the whole batch.
        """
        with self._tx() as db:
            return sum(self._insert(db, o, confirmed) for o in observations)

    def record_candidates(self, candidates, *, source_sha256: str, path: str) -> int:
        """Record :class:`discovery.Candidate` objects for one source file."""
        return self.record([Observation(c.kind, c.value, source_sha256, path, c.location, c.confidence, c.reason)
                            for c in candidates], confirmed=False)

    # ------------------------------------------------------------ aliases

    def _conflict(self, db: sqlite3.Connection, ctype: str, subject: str, other: str, detail: str) -> None:
        key = self._hmac("machina-deid/catalog/conflict", ctype, subject, other)
        db.execute("INSERT OR IGNORE INTO conflicts VALUES (?,?,?,?,?)", (key, ctype, subject, other, detail))

    def assert_alias(self, kind: str, canonical: str, alias: str) -> str:
        """Explicitly declare that ``alias`` is the same entity as ``canonical`` (same kind, exact values).

        Returns the canonical pseudonym. Contradictions raise :class:`CatalogError` after being recorded
        as a conflict (the conflict record commits; no link is made). Re-asserting is a no-op.
        """
        self._check_kind(kind)
        if _normalize(canonical) == _normalize(alias):
            raise CatalogError("alias and canonical normalize to the same identity")
        error: str | None = None
        with self._tx() as db:
            ckey, _ = self._ensure_identity(db, kind, canonical)
            akey, _ = self._ensure_identity(db, kind, alias)
            croot = self._root(db, ckey)
            current = db.execute("SELECT alias_of FROM identities WHERE id_key=?", (akey,)).fetchone()[0]
            if current is not None and self._root(db, akey) != croot:
                self._conflict(db, "alias_conflict", akey, ckey, "alias already linked to a different canonical")
                error = "alias conflicts with an existing alias assertion"
            elif croot == akey:
                self._conflict(db, "alias_cycle", akey, ckey, "assertion would create an alias cycle")
                error = "alias assertion would create a cycle"
            elif current is None:
                if db.execute("SELECT 1 FROM identities WHERE alias_of=?", (akey,)).fetchone():
                    self._conflict(db, "alias_conflict", akey, ckey, "alias is already canonical for others")
                    error = "alias is already canonical for other aliases"
                else:
                    db.execute("UPDATE identities SET alias_of=? WHERE id_key=?", (croot, akey))
            pseud = db.execute("SELECT pseudonym FROM identities WHERE id_key=?", (croot,)).fetchone()[0]
        if error:
            raise CatalogError(error)
        return pseud

    # ------------------------------------------------------------ reports

    def report(self) -> dict:
        """Machine-readable, redacted report: digests (truncated), pseudonyms, counts; no raw values."""
        db = self._db
        s = lambda k: k[:16] if k else None  # noqa: E731
        identities = [
            {"id": s(k), "kind": kind, "pseudonym": p, "alias_of": s(a),
             "observations": db.execute("SELECT COUNT(*) FROM observations WHERE id_key=?", (k,)).fetchone()[0]}
            for k, kind, p, a in db.execute("SELECT id_key, kind, pseudonym, alias_of FROM identities ORDER BY id_key")]
        unresolved = []
        for vkey, kinds, n, files in db.execute(
                """SELECT o.value_key, GROUP_CONCAT(DISTINCT o.kind), COUNT(*), COUNT(DISTINCT o.source_sha256)
                   FROM observations o WHERE o.id_key IS NULL
                   AND NOT EXISTS (SELECT 1 FROM identities i WHERE i.value_key = o.value_key)
                   GROUP BY o.value_key ORDER BY o.value_key"""):
            locs = [{"source_sha256": h[:16], "path_id": p[:16], "location": loc, "confidence": c, "reason": r}
                    for h, p, loc, c, r in db.execute(
                        "SELECT source_sha256, path_id, location, confidence, reason FROM observations "
                        "WHERE value_key=? AND id_key IS NULL ORDER BY source_sha256, path_id, location", (vkey,))]
            unresolved.append({"value": s(vkey), "candidate_kinds": sorted(kinds.split(",")), "observations": n,
                               "files": files, "locations": locs})
        ambiguities = [
            {"type": "same_value_multiple_kinds", "value": s(v), "kinds": sorted(k.split(","))}
            for v, k in db.execute("SELECT value_key, GROUP_CONCAT(kind) FROM identities GROUP BY value_key "
                                   "HAVING COUNT(*) > 1 ORDER BY value_key")]
        conflicts = [{"type": t, "subject": s(a), "other": s(b), "detail": d}
                     for t, a, b, d in db.execute("SELECT type, subject, other, detail FROM conflicts ORDER BY conflict_key")]
        n_obs = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        return {
            "schema_version": SCHEMA_VERSION,
            "secret_fingerprint": self._fingerprint,
            "summary": {"identities": len(identities), "aliases": sum(1 for i in identities if i["alias_of"]),
                        "observations": n_obs, "unresolved_candidates": len(unresolved),
                        "ambiguities": len(ambiguities), "conflicts": len(conflicts)},
            "identities": identities,
            "unresolved_candidates": unresolved,
            "ambiguities": ambiguities,
            "conflicts": conflicts,
        }
