"""Inventory exact approved identities in paths and verify known PDF file references.

Reports aggregate counts. The manifest is a closed file inventory, but opaque
CAD and archive references remain unknown. This diagnostic never moves files.
"""
from __future__ import annotations

import argparse
import json
import posixpath
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .manifest import parse_manifest
from .pdf_entities import Resolver

_PATH_KINDS = {"person", "org", "account", "email", "domain", "project"}


def _new_component(part: str, lookup) -> str:
    entry = lookup(part)
    if entry is None or entry.status != "ok" or entry.kind not in _PATH_KINDS:
        return part
    value = entry.pseudonym
    if (not value or value in {".", ".."} or value.endswith((" ", "."))
            or any(c in value for c in '/\\\\:*?"<>|') or any(ord(c) < 32 for c in value)):
        return part
    return value


def candidate_path(rel: str, lookup) -> str:
    path = PurePosixPath(rel)
    directories = [_new_component(part, lookup) for part in path.parts[:-1]]
    basename = _new_component(path.stem, lookup) + path.suffix
    return PurePosixPath(*directories, basename).as_posix()


def _target(owner: str, reference: str) -> str | None:
    reference = reference.replace("\\\\", "/")
    if not reference or reference.startswith("/") or ":" in reference:
        return None
    target = posixpath.normpath(posixpath.join(posixpath.dirname(owner), reference))
    return None if target == ".." or target.startswith("../") else target


def audit(root: Path, rels: list[str], lookup) -> dict[str, int]:
    import pymupdf

    paths = set(rels)
    plans = {rel: candidate_path(rel, lookup) for rel in sorted(paths)}
    changed = {rel for rel, target in plans.items() if rel != target}
    insensitive = Counter(p.casefold() for p in plans.values())
    stats = Counter(
        files=len(paths),
        exact_identity_path_candidates=len(changed),
        path_candidates_with_collision=sum(insensitive[plans[p].casefold()] > 1 for p in changed),
    )
    backrefs = set()
    for rel in sorted(paths):
        if PurePosixPath(rel).suffix.lower() != ".pdf":
            stats["files_with_unaudited_reference_formats"] += 1
            continue
        try:
            with pymupdf.open(root / rel) as doc:
                if doc.needs_pass:
                    stats["pdf_reference_read_failures"] += 1
                    continue
                for page in doc:
                    for link in page.get_links():
                        reference = link.get("file")
                        if not reference:
                            continue
                        xref = link.get("xref")
                        if xref:
                            kind, raw = doc.xref_get_key(xref, "A/F/UF")
                            if kind != "string":
                                kind, raw = doc.xref_get_key(xref, "A/F/F")
                            reference = raw if kind == "string" else unquote(reference)
                        target = _target(rel, reference)
                        if target is None:
                            stats["pdf_file_refs_external_or_unsafe"] += 1
                        elif target not in paths:
                            stats["pdf_file_refs_target_missing"] += 1
                        else:
                            stats["pdf_file_refs_target_present"] += 1
                            if target in changed:
                                backrefs.add(target)
        except (OSError, RuntimeError, ValueError):
            stats["pdf_reference_read_failures"] += 1
    stats["candidate_targets_with_incoming_pdf_file_refs"] = len(backrefs)
    stats["candidates_requiring_reference_review"] = len(changed)
    return dict(sorted(stats.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--seed-file", type=Path, required=True)
    args = parser.parse_args()
    rows, rejected = parse_manifest(args.manifest)
    rels = [row.path for row in rows]
    if rejected or any(PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts for rel in rels):
        parser.error("invalid manifest paths")
    resolver = Resolver(args.seed_file.read_bytes(), args.map)
    try:
        print(json.dumps(audit(args.input, rels, resolver.entry), sort_keys=True))
    finally:
        resolver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
