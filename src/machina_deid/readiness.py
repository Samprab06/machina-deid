"""Storage-independent full-corpus readiness accounting.

This module deliberately does not read source files while building a readiness
plan.  A manifest row is the unit of accounting, including rows that share a
source hash.  Local processing can attach terminal results later through a
small, atomic JSON checkpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass

from pathlib import Path, PurePosixPath
from typing import Iterable

from .state import StateError, output_path_for

TERMINAL_OUTCOMES = (
    "transformed_validated",
    "inspected_no_identifiers",
    "unsupported_passthrough",
    "identifiers_unmodifiable",
    "failed_validation",
)
# Organizer rule: "Any single file over 2 GB in corpus/ is skipped during grading."
# "GB" is read conservatively as decimal (2e9 bytes), which also satisfies 2 GiB.
MAX_FILE_BYTES = 2_000_000_000
# Organizer rule: a corpus/ with more than 400,000 objects is rejected.
MAX_CORPUS_OBJECTS = 400_000
# Official attempt layout: attempt-N/{corpus/, code/, README.txt}.
CODE_DIR = "code"
LEGACY_CODE_DIR = "pipeline-source"  # older local dry-run layouts
HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ManifestEntry:
    """A valid manifest row, identified by its one-based physical row number."""

    row_number: int
    path: str
    size: int
    source_hash: str = ""

    @property
    def row_id(self) -> str:
        return str(self.row_number)

    @property
    def source_hash_key(self) -> str:
        return self.source_hash.lower() or ""


def read_entries(manifest: str | Path) -> tuple[list[ManifestEntry], int]:
    """Read valid rows while retaining duplicate paths as distinct row IDs."""
    entries: list[ManifestEntry] = []
    malformed = 0
    physical = 0
    with Path(manifest).open("r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        for line in fh:
            physical += 1
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            try:
                size = int(cols[1])
                if size < 0 or not cols[0]:
                    raise ValueError
                entries.append(ManifestEntry(physical, cols[0], size,
                                             cols[2].strip().lower() if len(cols) > 2 else ""))
            except (IndexError, ValueError):
                malformed += 1
    return entries, malformed


def _path_counts(entries: Iterable[ManifestEntry]) -> dict[str, int]:
    c = Counter(e.path for e in entries)
    return dict(sorted(c.items()))


def _manifest_digest(entries: Iterable[ManifestEntry]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(f"{e.row_number}\t{e.path}\t{e.size}\t{e.source_hash}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def new_report(manifest: str | Path, output_root: str | Path = "corpus") -> dict:
    """Create a complete, path-safe report without requiring corpus storage."""
    entries, malformed = read_entries(manifest)
    duplicates = {p for p, n in _path_counts(entries).items() if n > 1}
    rows = []
    for e in entries:
        duplicate = e.path in duplicates
        try:
            # The report stores the path relative to corpus/, while output_root
            # documents the physical root selected by the caller.
            output_path_for("/unused", e.path)
            output = PurePosixPath(e.path).as_posix()
        except StateError:
            output = None
            duplicate = True
        rows.append({
            "row_id": e.row_id, "manifest_row": e.row_number, "source_path": e.path,
            "source_bytes": e.size, "source_hash": e.source_hash, "output_path": output,
            "outcome": "failed_validation" if duplicate else None,
            "error": "duplicate_manifest_path" if e.path in duplicates else ("unsafe_manifest_path" if output is None else None),
            "output_sha256": None, "output_bytes": None,
        })
    hash_groups: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        if e.source_hash:
            hash_groups[e.source_hash].append(e.row_id)
    return {
        "schema": 1, "manifest_sha256": _manifest_digest(entries),
        "manifest_rows": len(entries), "malformed_manifest_lines": malformed,
        "output_root": str(output_root), "terminal_outcomes": list(TERMINAL_OUTCOMES),
        "duplicate_manifest_paths": sorted(duplicates),
        "duplicate_source_hashes": {k: v for k, v in sorted(hash_groups.items()) if len(v) > 1},
        "rows": rows,
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_checkpoint(report: dict, path: str | Path) -> Path:
    """Write a resumable checkpoint atomically and return its path."""
    p = Path(path)
    _atomic_json(p, report)
    return p


def read_checkpoint(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        report = json.load(fh)
    if report.get("schema") != 1 or not isinstance(report.get("rows"), list):
        raise ValueError("unsupported readiness checkpoint")
    return report


def record_batch(report: dict, results: Iterable[dict], checkpoint: str | Path | None = None) -> dict:
    """Apply terminal results exactly once, optionally checkpointing immediately.

    Each result needs ``row_id`` and ``outcome``.  Repeating an already terminal
    row is accepted only when the outcome and output mapping are identical.
    """
    by_id = {str(r["row_id"]): r for r in report["rows"]}
    changed = 0
    for result in results:
        rid = str(result.get("row_id"))
        if rid not in by_id:
            raise ValueError(f"unknown manifest row: {rid}")
        outcome = result.get("outcome")
        if outcome not in TERMINAL_OUTCOMES:
            raise ValueError(f"invalid terminal outcome: {outcome!r}")
        row = by_id[rid]
        updates = {k: result[k] for k in ("output_path", "output_sha256", "output_bytes", "error") if k in result}
        if row["outcome"] is not None:
            if row["outcome"] != outcome or any(row.get(k) != v for k, v in updates.items()):
                raise ValueError(f"manifest row already completed: {rid}")
            continue
        if outcome != "failed_validation" and not result.get("output_path"):
            raise ValueError(f"terminal output mapping required for {rid}")
        if "output_path" in updates:
            p = PurePosixPath(str(updates["output_path"]))
            if p.is_absolute() or ".." in p.parts or not p.parts or any(part in ("", ".") for part in p.parts):
                raise ValueError(f"unsafe output path for {rid}")
        row.update(updates)
        row["outcome"] = outcome
        changed += 1
    if checkpoint is not None:
        write_checkpoint(report, checkpoint)
    return {"updated": changed, **outcome_counts(report)}


def outcome_counts(report: dict) -> dict[str, int]:
    counts = {k: 0 for k in TERMINAL_OUTCOMES}
    counts["pending"] = 0
    for row in report.get("rows", []):
        outcome = row.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
        else:
            counts["pending"] += 1
    return counts


def validate_report(report: dict) -> list[str]:
    """Validate one-to-one row/output accounting without touching storage."""
    errors: list[str] = []
    rows = report.get("rows", [])
    ids = [str(r.get("row_id")) for r in rows]
    if len(ids) != len(set(ids)):
        errors.append("duplicate row IDs")
    paths = [r.get("output_path") for r in rows if r.get("output_path")]
    if len(paths) != len(set(paths)):
        errors.append("duplicate output paths")
    for row in rows:
        if row.get("outcome") not in (None, *TERMINAL_OUTCOMES):
            errors.append(f"invalid outcome for row {row.get('row_id')}")
        if row.get("outcome") and row["outcome"] != "failed_validation" and not row.get("output_path"):
            errors.append(f"missing output mapping for row {row.get('row_id')}")
    if report.get("manifest_rows") != len(rows):
        errors.append("manifest row count mismatch")
    return errors


def sha256_file(path: str | Path, chunk_bytes: int = HASH_CHUNK_BYTES) -> str:
    """Hash a file in bounded memory (corpus files may approach 2 GB)."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_bytes), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_code_dir(layout_root: str | Path) -> tuple[Path | None, list[str], list[str]]:
    """Return ``(local code dir, errors, warnings)``.

    ``code/`` is the official name.  A legacy ``pipeline-source/`` dry-run layout is
    accepted with a warning and is still uploaded to the remote ``code/`` prefix.
    Having both is ambiguous and is rejected so the wrong tree is never uploaded.
    """
    root = Path(layout_root)
    code, legacy = root / CODE_DIR, root / LEGACY_CODE_DIR
    if code.exists() and legacy.exists():
        return None, [f"ambiguous code tree: both {CODE_DIR}/ and {LEGACY_CODE_DIR}/ exist"], []
    for p, warn in ((code, []), (legacy, [f"legacy {LEGACY_CODE_DIR}/ used; it uploads as {CODE_DIR}/"])):
        if p.exists() or p.is_symlink():
            if p.is_symlink() or not p.is_dir():
                return None, [f"{p.name} is not a real directory"], []
            return p, [], warn
    return None, [f"missing {CODE_DIR}"], []


def validate_final_layout(layout_root: str | Path, manifest: str | Path, *, report: dict | None = None,
                          max_file_bytes: int = MAX_FILE_BYTES) -> dict:
    """Validate the submission layout and, when present, every recorded output hash."""
    root = Path(layout_root)
    entries, malformed = read_entries(manifest)
    expected = {e.path: e for e in entries}
    duplicates = sorted(p for p, n in _path_counts(entries).items() if n > 1)
    errors: list[str] = []
    _code, code_errors, warnings = resolve_code_dir(root)
    errors.extend(code_errors)
    if not (root / "README.txt").is_file():
        errors.append("missing README.txt")
    if not (root / "corpus").is_dir():
        errors.append("missing corpus")
    corpus = root / "corpus"
    actual: dict[str, Path] = {}
    if corpus.is_dir():
        for p in corpus.rglob("*"):
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(corpus).as_posix()
            if rel in actual:
                errors.append(f"duplicate corpus path: {rel}")
            actual[rel] = p
            if p.stat().st_size > max_file_bytes:
                errors.append(f"file exceeds limit: {rel}")
    if len(actual) > MAX_CORPUS_OBJECTS:
        errors.append(f"corpus exceeds {MAX_CORPUS_OBJECTS} objects: {len(actual)}")
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if duplicates:
        errors.append("duplicate manifest paths: " + ", ".join(duplicates))
    if missing:
        errors.append("missing manifest paths: " + ", ".join(missing))
    if extra:
        errors.append("unlisted corpus paths: " + ", ".join(extra))
    if malformed:
        errors.append(f"malformed manifest lines: {malformed}")
    if report:
        errors.extend(validate_report(report))
        for row in report.get("rows", []):
            if row.get("outcome") == "failed_validation" or not row.get("output_sha256"):
                continue
            p = root / "corpus" / str(row["output_path"])
            if not p.is_file():
                errors.append(f"missing recorded output: {row['output_path']}")
            else:
                digest = sha256_file(p)
                if digest != row["output_sha256"]:
                    errors.append(f"hash mismatch: {row['output_path']}")
        counts = outcome_counts(report)
        if counts["pending"]:
            errors.append(f"pending manifest rows: {counts['pending']}")
        if counts["failed_validation"]:
            errors.append(f"failed manifest rows: {counts['failed_validation']}")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "manifest_rows": len(entries),
            "corpus_files": len(actual), "missing_paths": missing, "extra_paths": extra,
            "duplicate_manifest_paths": duplicates, "max_file_bytes": max_file_bytes,
            "outcome_counts": outcome_counts(report) if report else None}


def upload_plan(layout_root: str | Path, destination: str) -> dict:
    """Produce a credential-free command plan for the supplied ``upload.py``."""
    root = Path(layout_root)
    code, errors, warnings = resolve_code_dir(root)
    if errors:
        raise ValueError("; ".join(errors))
    if not destination.startswith("gs://") or not destination.rstrip("/").rsplit("/", 1)[-1].startswith("attempt-"):
        raise ValueError("destination must be gs://.../attempt-N/ (the folder holding corpus/, not corpus/ itself)")
    if not destination.endswith("/"):
        destination += "/"
    return {
        "destination": destination,
        "credentials_required": False,
        "executed": False,
        "upload_script": "upload.py",
        "paths": {"corpus": str(root / "corpus"), "code": str(code), "readme": str(root / "README.txt")},
        "remote": {"corpus": destination + "corpus/", "code": destination + "code/",
                   "readme": destination + "README.txt"},
        "warnings": warnings,
        "argv": ["python3", "upload.py", "--dest", destination, "--corpus", str(root / "corpus"),
                 "--code", str(code), "--readme", str(root / "README.txt")],
        "note": "Plan only; supply MACHINA_UPLOAD_TOKEN explicitly when authorized.",
    }


# Descriptive aliases keep the API discoverable for callers that think in
# terms of a coordinator/report rather than a checkpoint implementation.
build_report = new_report
apply_batch = record_batch
validate_layout = validate_final_layout
generate_upload_plan = upload_plan
