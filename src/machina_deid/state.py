"""Durable SQLite processing state.

Each record carries an explicit ``source_physical_path`` supplied by the
caller; it is never derived from the output path. This module never deletes or
modifies source files. Output paths are produced by :func:`output_path_for`,
which maps a relative source path under an output root (case-sensitive) and
rejects traversal.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

PENDING, IN_PROGRESS, TRANSFORMED, VALIDATED, FAILED, SKIPPED = (
    "pending", "in_progress", "transformed", "validated", "failed", "skipped")
STATUSES = (PENDING, IN_PROGRESS, TRANSFORMED, VALIDATED, FAILED, SKIPPED)
_TRANSITIONS = {
    PENDING: {IN_PROGRESS, SKIPPED},
    IN_PROGRESS: {TRANSFORMED, FAILED, PENDING, SKIPPED},
    TRANSFORMED: {VALIDATED, FAILED},
    VALIDATED: set(),
    FAILED: {PENDING},
    SKIPPED: {PENDING},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  manifest_path TEXT NOT NULL UNIQUE,
  source_rel_path TEXT NOT NULL,
  source_physical_path TEXT NOT NULL,
  input_size INTEGER,
  input_sha256 TEXT,
  classification TEXT,
  transformer TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  output_path TEXT,
  output_sha256 TEXT,
  validation_status TEXT,
  error_category TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status, id);
CREATE TABLE IF NOT EXISTS content (
  input_sha256 TEXT NOT NULL,
  variant TEXT NOT NULL,
  cas_path TEXT NOT NULL,
  output_sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  disposition TEXT NOT NULL,
  note TEXT,
  validation_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (input_sha256, variant)
);
"""

# Columns added after the first release; applied idempotently to older databases.
_MIGRATIONS = (("disposition", "TEXT"), ("note", "TEXT"), ("link_kind", "TEXT"))


class StateError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_relative(rel: str) -> PurePosixPath:
    raw_parts = rel.replace("\\", "/").split("/")
    if any(part in {"", "."} for part in raw_parts):
        raise StateError("unsafe relative path")
    p = PurePosixPath(rel)
    segs = rel.split("/")
    if p.is_absolute() or not p.parts or "\x00" in rel or any(s in ("", ".", "..") for s in segs):
        raise StateError("unsafe relative path")
    return p


def output_path_for(output_root: str | Path, source_rel_path: str) -> Path:
    """Map a relative source path to ``output_root/<rel>`` (case preserved)."""
    return Path(output_root).joinpath(*safe_relative(source_rel_path).parts)


class StateDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(records)")}
        for col, typ in _MIGRATIONS:
            if col not in have:
                self.conn.execute(f"ALTER TABLE records ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def register(self, manifest_path: str, source_rel_path: str, source_physical_path: str | Path, *,
                 input_size: int | None = None, input_sha256: str | None = None,
                 classification: str | None = None, transformer: str | None = None) -> bool:
        """Insert a record; return False if the manifest path already exists (state kept)."""
        safe_relative(source_rel_path)
        phys = Path(source_physical_path)
        if not phys.is_absolute():
            raise StateError("source_physical_path must be absolute and explicit")
        now = _now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO records(manifest_path, source_rel_path, source_physical_path, input_size,"
                " input_sha256, classification, transformer, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (manifest_path, source_rel_path, str(phys), input_size, input_sha256, classification,
                 transformer, now, now))
            return cur.rowcount == 1

    def get(self, record_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise StateError("no such record")
        return row

    def counts(self) -> dict[str, int]:
        d = {s: 0 for s in STATUSES}
        for r in self.conn.execute("SELECT status, COUNT(*) n FROM records GROUP BY status"):
            d[r["status"]] = r["n"]
        return d

    def select_batch(self, max_files: int, max_bytes: int | None = None,
                     transformer: str | None = None, include_untransformable: bool = False) -> list[sqlite3.Row]:
        """Atomically claim up to ``max_files``/``max_bytes`` pending records (-> in_progress).

        Only records with a transformer are claimable unless ``include_untransformable``
        (used by the pass-through orchestrator). Records that would exceed
        ``max_bytes`` are left pending, never claimed.
        """
        if max_files <= 0:
            raise StateError("max_files must be positive")
        picked, total = [], 0
        with self._tx() as c:
            q = "SELECT id, input_size FROM records WHERE status=?"
            if not include_untransformable:
                q += " AND transformer IS NOT NULL"
            args: list = [PENDING]
            if transformer:
                q += " AND transformer=?"
                args.append(transformer)
            for row in c.execute(q + " ORDER BY id", args).fetchall():
                size = row["input_size"] or 0
                if max_bytes is not None and total + size > max_bytes:
                    continue
                picked.append(row["id"])
                total += size
                if len(picked) >= max_files:
                    break
            now = _now()
            for rid in picked:
                c.execute("UPDATE records SET status=?, attempts=attempts+1, started_at=?, updated_at=?,"
                          " error_category=NULL WHERE id=?", (IN_PROGRESS, now, now, rid))
        return [self.get(r) for r in picked]

    def _transition(self, rid: int, new: str, **fields) -> None:
        with self._tx() as c:
            row = c.execute("SELECT status, source_physical_path FROM records WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise StateError("no such record")
            if new not in _TRANSITIONS[row["status"]]:
                raise StateError(f"illegal transition {row['status']} -> {new}")
            if fields.get("output_path") is not None and Path(fields["output_path"]) == Path(row["source_physical_path"]):
                raise StateError("output path equals source path")
            now = _now()
            fields.update(status=new, updated_at=now)
            if new in (TRANSFORMED, VALIDATED, FAILED, SKIPPED):
                fields["finished_at"] = now
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE records SET {cols} WHERE id=?", (*fields.values(), rid))

    def mark_transformed(self, rid: int, output_path: str | Path, output_sha256: str, **extra) -> None:
        self._transition(rid, TRANSFORMED, output_path=str(output_path), output_sha256=output_sha256, **extra)

    def mark_validated(self, rid: int, validation_status: str = "ok") -> None:
        self._transition(rid, VALIDATED, validation_status=validation_status)

    def set_input_sha256(self, rid: int, sha256: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE records SET input_sha256=?, updated_at=? WHERE id=? AND status=?",
                      (sha256, _now(), rid, IN_PROGRESS))

    def get_content(self, input_sha256: str, variant: str):
        return self.conn.execute("SELECT * FROM content WHERE input_sha256=? AND variant=?",
                                 (input_sha256, variant)).fetchone()

    def put_content(self, input_sha256: str, variant: str, cas_path: str | Path, output_sha256: str, size: int,
                    disposition: str, note: str | None, validation_status: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO content VALUES (?,?,?,?,?,?,?,?,?)",
                      (input_sha256, variant, str(cas_path), output_sha256, size, disposition, note,
                       validation_status, _now()))

    def mark_failed(self, rid: int, error_category: str, validation_status: str | None = None) -> None:
        self._transition(rid, FAILED, error_category=error_category, validation_status=validation_status)

    def mark_skipped(self, rid: int, reason: str) -> None:
        self._transition(rid, SKIPPED, error_category=reason)

    def requeue_failed(self) -> int:
        """Return failed/skipped records to pending (explicit retry). Validated records are never touched."""
        n = 0
        for r in self.conn.execute("SELECT id FROM records WHERE status IN (?,?)", (FAILED, SKIPPED)).fetchall():
            self.requeue(r["id"])
            n += 1
        return n

    def requeue(self, rid: int) -> None:
        self._transition(rid, PENDING, error_category=None, output_path=None, output_sha256=None,
                         validation_status=None)

    def recover_interrupted(self) -> int:
        """Return records left ``in_progress`` by a crash to ``pending`` (their outputs are not trusted)."""
        with self._tx() as c:
            cur = c.execute("UPDATE records SET status=?, error_category='interrupted', updated_at=? WHERE status=?",
                            (PENDING, _now(), IN_PROGRESS))
            return cur.rowcount
