#!/usr/bin/env python3
"""Phase 5G: private candidate review tooling (local, protected context only).

Subcommands:

  report  Verify the frozen benchmark selection/checksums, rediscover candidates from the selected real
          files (twopass.discover_file, the production discovery path), and group them by the catalog's
          normalized identity key (``IdentityCatalog.value_key``: NFKC + casefold + whitespace collapse,
          keyed with the catalog secret). Each group lists semantic kinds, location categories and exact
          locations, observed formats, confidences, occurrence count, source sample ids, evidence
          locations, and the *current* catalog decisions + pseudonym status. Nothing is recorded or
          decided: the catalog is only read.

          Two files are written under ``/srv/new2/machina-deid/work/<bench>/review/<name>/`` (0700 dirs):
            private-review.json  0600; the ONLY artifact holding raw candidate values / member paths.
            public-summary.json  0600; opaque identity digests and counts only (checked fail-closed).
          Stdout shows the public summary. ``--show-private`` is required to print raw values.

  decide  Record explicit typed manual decisions via ``IdentityCatalog.decide`` (source="manual").
          Identities are referenced by their opaque digest from a private review report, never by raw
          value on argv. Decisions come from argv (``--digest/--kind/--decision/--reason``) or from stdin
          (``--stdin``; one ``<digest> <kind> <approve|reject|review> <reason...>`` per line, ``#`` comments
          allowed). The report must be a 0600 file owned by the current user under the work root.

Typical workflow (nothing here ever touches Git)::

  scripts/private_review.py report --bench phase5e-balanced --catalog <run>/catalog/catalog.sqlite
  scripts/private_review.py report ... --show-private | less         # inspect raw values locally
  scripts/private_review.py decide --bench phase5e-balanced --catalog ... --report <private-review.json> \\
      --digest 0123abcd... --kind person --decision approve --reason "reviewed: author field"
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import sqlite3
import stat
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from machina_deid import twopass as tp  # noqa: E402
from machina_deid.catalog import APPROVAL_STATUSES, CatalogError, IdentityCatalog  # noqa: E402
from machina_deid.identity import KINDS, _normalize  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("phase5e_balanced_benchmark",
                                               REPO / "scripts" / "phase5e_balanced_benchmark.py")
bm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bm)

WORK_ROOT = Path("/srv/new2/machina-deid/work")
REPORT_VERSION = 2
DIGEST_LEN = 16
DECISIONS = {"approve": "approved", "reject": "rejected", "review": "review"}
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_DIGEST = re.compile(rf"[0-9a-f]{{{DIGEST_LEN}}}")


# ------------------------------------------------------------------ protected paths


def _within_work(p: Path) -> Path:
    """Resolve ``p`` and require it inside WORK_ROOT and outside the Git repository."""
    p, root = p.absolute(), WORK_ROOT.absolute()
    if not p.is_relative_to(root) or p == root or ".." in p.relative_to(root).parts:
        raise SystemExit("refusing: path must be inside the work root")
    for entry in (p, *p.parents):
        if entry == root:
            break
        try:
            st = entry.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o022 or (entry != p and not stat.S_ISDIR(st.st_mode))):
            raise SystemExit("refusing: unsafe work path")
    r = p.resolve()
    if r != p or bm._is_within(r, REPO) or bm._is_within(REPO, r):
        raise SystemExit("refusing: work path must be direct and outside the Git repository")
    return p


def _check_private_file(p: Path) -> None:
    try:
        st = p.lstat()
    except OSError:
        raise SystemExit("refusing: required private file does not exist") from None
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1):
        raise SystemExit("refusing: private file must be a singly linked regular file owned by you with mode 0600")


def _write_new_private(p: Path, data: dict) -> None:
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=2, sort_keys=True)


def _report_mac(report: dict, secret: bytes) -> str:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret, b"machina-deid/private-review/v2\x00" + payload, hashlib.sha256).hexdigest()


def _frozen(bench: Path) -> tuple[dict, dict, dict]:
    selection, checksums = bench / "selection.json", bench / "checksums.json"
    for path in (selection, checksums):
        _check_private_file(_within_work(path))
    sel = bm.load_selection(bench)
    try:
        checks = json.loads(checksums.read_text(encoding="utf-8"))
        ids = [f["id"] for f in sel["files"]]
        if len(ids) != len(checks) or set(ids) != checks.keys() or bm.verify_frozen(sel["files"], checks):
            raise SystemExit("refusing: frozen source or checksum entry missing or changed")
    except (KeyError, TypeError, ValueError, OSError):
        raise SystemExit("refusing: invalid frozen checksums") from None
    return sel, checks, {"selection_sha256": bm.sha256_file(selection)[:16],
                         "checksums_sha256": bm.sha256_file(checksums)[:16], "frozen_verified": True}


def _bench(name: str) -> Path:
    if not _NAME.fullmatch(name) or name.startswith("."):
        raise SystemExit("invalid --bench name")
    return _within_work(WORK_ROOT / name)


def open_catalog(catalog: Path, secret_path: Path, *, read_only: bool = False) -> tuple[IdentityCatalog, bytes]:
    catalog, secret_path = _within_work(catalog), _within_work(secret_path)
    for path in (catalog, secret_path):
        _check_private_file(path)
    for path in (catalog.with_suffix(".pseudonyms.json"), Path(f"{catalog}-wal"), Path(f"{catalog}-shm"),
                 Path(f"{catalog}-journal")):
        _within_work(path)
        if path.exists():
            _check_private_file(path)
    secret = secret_path.read_bytes().strip()
    try:
        return IdentityCatalog(catalog, secret, read_only=read_only), secret
    except (CatalogError, sqlite3.Error, OSError):
        raise SystemExit("refusing: could not open the protected catalog with this secret") from None


# ------------------------------------------------------------------ grouping


def digest(value_key: str) -> str:
    return value_key[:DIGEST_LEN]


def catalog_state(cat: IdentityCatalog, value: str) -> dict:
    """Current decisions for this exact normalized value under every kind (read only)."""
    decisions = {k: s for k in sorted(KINDS) if (s := cat.approval_status(k, value)) is not None}
    ap = cat.approved_pseudonym(value)
    return {"decisions": decisions,
            "pseudonym_status": "approved" if ap else ("undecided" if not decisions else "not_approved"),
            "pseudonym_kind": ap[0] if ap else None, "pseudonym": ap[1] if ap else None}


def collect(files: list[dict], checks: dict) -> tuple[list[tuple[str, object]], Counter]:
    """Rediscover candidates -> ``[(sample_id, DiscoveredFile)]`` + per-status counts."""
    out, stats = [], Counter()
    for e in files:
        c = checks.get(e["id"])
        if c is None:
            stats["not_frozen"] += 1
            continue
        try:
            found = tp.discover_file(Path(c["path"]), e["id"])
        except Exception as exc:  # noqa: BLE001
            stats[f"failed:discovery_error:{type(exc).__name__}"] += 1
            continue
        for d in found:
            stats[d.status] += 1
            if d.status == "ok":
                out.append((e["id"], d))
    return out, stats


def group_candidates(cat: IdentityCatalog, discovered: list) -> dict[str, dict]:
    """Group raw candidates by catalog value key. Returns private groups keyed by opaque digest."""
    groups: dict[str, dict] = {}
    acc: dict[str, dict] = defaultdict(lambda: {"kinds": Counter(), "location_categories": Counter(),
                                                "formats": Counter(), "confidences": Counter(),
                                                "samples": set(), "evidence": [], "values": Counter(),
                                                "auto_policy": Counter()})
    for sample_id, d in discovered:
        for c in d.candidates:
            try:
                vkey = cat.value_key(c.value)
            except CatalogError:
                continue
            g = acc[vkey]
            g["kinds"][c.kind] += 1
            g["location_categories"][bm.location_category(c.location)] += 1
            g["formats"][d.format or "unknown"] += 1
            g["confidences"][c.confidence] += 1
            g["samples"].add(sample_id)
            g["values"][c.value] += 1
            g["auto_policy"][tp.auto_decision(c)[0]] += 1
            g["evidence"].append({"sample_id": sample_id, "member": d.path, "format": d.format,
                                  "location": c.location, "kind": c.kind, "confidence": c.confidence,
                                  "reason": c.reason})
    for vkey, g in acc.items():
        rep = g["values"].most_common(1)[0][0]
        dg = digest(vkey)
        if dg in groups:  # pragma: no cover - 64-bit prefix collision
            raise SystemExit("digest collision; increase DIGEST_LEN")
        groups[dg] = {
            "digest": dg,
            "raw_values": dict(sorted(g["values"].items())),  # exact spellings seen (PRIVATE)
            "normalized_length": len(_normalize(rep)),
            "kinds": dict(sorted(g["kinds"].items())),
            "location_categories": dict(sorted(g["location_categories"].items())),
            "formats": dict(sorted(g["formats"].items())),
            "confidences": dict(sorted(g["confidences"].items())),
            "occurrences": sum(g["kinds"].values()),
            "source_samples": sorted(g["samples"]),
            "evidence": sorted(g["evidence"], key=lambda x: (x["sample_id"], x["member"], x["location"])),
            "auto_policy": dict(sorted(g["auto_policy"].items())),
            "catalog": catalog_state(cat, rep),
        }
    return dict(sorted(groups.items(), key=lambda kv: (-kv[1]["occurrences"], kv[0])))


def public_summary(groups: dict[str, dict], stats: Counter, meta: dict) -> dict:
    """Opaque digests and counts only (category labels are fixed vocabularies, never values)."""
    status = Counter()
    for g in groups.values():
        status[g["catalog"]["pseudonym_status"]] += 1
    return {
        "phase": "5G", **meta,
        "entries": dict(sorted(stats.items())),
        "groups": len(groups),
        "occurrences": sum(g["occurrences"] for g in groups.values()),
        "groups_by_pseudonym_status": dict(sorted(status.items())),
        "items": [{"digest": d, "occurrences": g["occurrences"], "samples": len(g["source_samples"]),
                   "kinds": g["kinds"], "formats": g["formats"], "confidences": g["confidences"],
                   "decision_counts": dict(Counter(g["catalog"]["decisions"].values())),
                   "pseudonym_status": g["catalog"]["pseudonym_status"]}
                  for d, g in groups.items()],
        "raw_values_in_summary": False,
    }


def assert_public(summary: dict, groups: dict[str, dict], sel: dict, checks: dict) -> None:
    """Fail closed if any raw value, member path or private path would reach the public summary."""
    text = json.dumps(summary)
    leaked = 0
    for g in groups.values():
        for v in g["raw_values"]:
            if len(v.strip()) >= 2 and (json.dumps(v)[1:-1] in text or v in text):
                leaked += 1
        for ev in g["evidence"]:
            if ev["member"] != ev["sample_id"] and ev["member"] in text:
                leaked += 1
    try:
        bm.assert_redacted(summary, sel, checks)
    except SystemExit:
        leaked += 1
    if leaked:
        raise SystemExit(f"refusing: public summary would leak {leaked} private string(s)")


# ------------------------------------------------------------------ report


def do_report(bench_name: str, catalog: Path, secret: Path | None, name: str | None) -> tuple[dict, dict, Path]:
    bench = _bench(bench_name)
    sel, checks, meta = _frozen(bench)
    name = name or time.strftime("%Y%m%dT%H%M%S")
    if not _NAME.fullmatch(name) or name.startswith("."):
        raise SystemExit("invalid --name")
    out_dir = _within_work(bench / "review" / name)
    bm.check_no_overlap(bench, [Path(c["path"]) for c in checks.values()], [out_dir])
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit("refusing: review dir is not empty")
    cat, key = open_catalog(catalog, secret or bench / "catalog.secret", read_only=True)
    with cat:
        discovered, stats = collect(sel["files"], checks)
        groups = group_candidates(cat, discovered)
    if _frozen(bench)[2] != meta:
        raise SystemExit("refusing: frozen inputs changed during review")
    summary = public_summary(groups, stats, meta)
    assert_public(summary, groups, sel, checks)
    private = {"version": REPORT_VERSION, "phase": "5G", "WARNING": "PRIVATE: contains raw identity values",
               **meta, "catalog_path": str(_within_work(catalog)), "bench": bench_name,
               "entries": dict(sorted(stats.items())), "groups": groups}
    private["authentication"] = _report_mac(private, key)
    out_dir.parent.mkdir(mode=0o700, exist_ok=True)
    if stat.S_IMODE(out_dir.parent.lstat().st_mode) != 0o700:
        raise SystemExit("refusing: review parent must have mode 0700")
    try:
        out_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit("refusing: review dir already exists") from None
    _write_new_private(out_dir / "private-review.json", private)
    _write_new_private(out_dir / "public-summary.json", summary)
    return summary, private, out_dir


# ------------------------------------------------------------------ decisions


def parse_decision_lines(text: str) -> list[tuple[str, str, str, str]]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 3)
        if len(parts) != 4:
            raise SystemExit(f"line {n}: expected '<digest> <kind> <approve|reject|review> <reason>'")
        out.append(tuple(parts))
    return out


def validate_decision(dg: str, kind: str, decision: str, reason: str) -> None:
    if not _DIGEST.fullmatch(dg):
        raise SystemExit(f"invalid digest (expected {DIGEST_LEN} hex chars)")
    if kind not in KINDS:
        raise SystemExit(f"invalid kind (allowed: {', '.join(sorted(KINDS))})")
    if decision not in DECISIONS:
        raise SystemExit(f"invalid decision (allowed: {', '.join(DECISIONS)})")
    if not reason.strip():
        raise SystemExit("a reason is required")


def do_decide(bench_name: str, catalog: Path, secret: Path | None, report: Path,
              decisions: list[tuple[str, str, str, str]]) -> list[dict]:
    """Apply explicit typed manual decisions. Output holds digests/kinds/statuses only."""
    bench = _bench(bench_name)
    report = _within_work(report)
    _check_private_file(report)
    if not report.is_relative_to(bench / "review") or report.name != "private-review.json":
        raise SystemExit("refusing: report must be inside this benchmark's review directory")
    for directory in (report.parent, bench / "review"):
        if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
            raise SystemExit("refusing: review directories must have mode 0700")
    try:
        priv = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit("refusing: invalid private review report") from None
    if not isinstance(priv, dict) or priv.get("version") != REPORT_VERSION or not isinstance(priv.get("groups"), dict):
        raise SystemExit("refusing: not a current private review report")
    for d in decisions:
        validate_decision(*d)
        if d[0] not in priv["groups"]:
            raise SystemExit("refusing: digest not in the private review report")
    catalog = _within_work(catalog)
    if priv.get("catalog_path") != str(catalog) or priv.get("bench") != bench_name:
        raise SystemExit("refusing: report belongs to a different catalog or benchmark")
    sel, checks, meta = _frozen(bench)
    if any(priv.get(k) != v for k, v in meta.items()):
        raise SystemExit("refusing: report belongs to a different frozen selection")
    auth = priv.pop("authentication", None)
    cat, key = open_catalog(catalog, secret or bench / "catalog.secret", read_only=True)
    prepared = []
    with cat:
        if not isinstance(auth, str) or not hmac.compare_digest(auth.encode(), _report_mac(priv, key).encode()):
            raise SystemExit("refusing: report authentication failed")
        groups = priv["groups"]
        private_strings = {_normalize(v) for g in groups.values() for v in g["raw_values"]}
        private_strings.update(_normalize(ev["member"]) for g in groups.values() for ev in g["evidence"])
        private_strings.update(_normalize(c["path"]) for c in checks.values())
        private_strings.update(_normalize(e["manifest_path"]) for e in sel["files"])
        private_strings.add(_normalize(str(catalog)))
        seen = set()
        for dg, kind, decision, reason in decisions:
            values = groups[dg]["raw_values"]
            value = max(values, key=lambda v: (values[v], v))
            try:
                if digest(cat.value_key(value)) != dg:
                    raise CatalogError("digest mismatch")
                cat._check_reason(reason, value)
            except CatalogError:
                raise SystemExit("refusing: invalid digest or decision reason") from None
            norm = _normalize(reason)
            if any(v and (v in norm if len(v) >= 3 else v == norm) for v in private_strings):
                raise SystemExit("refusing: decision reason contains a private string")
            if (dg, kind) in seen:
                raise SystemExit("refusing: duplicate typed decision in batch")
            seen.add((dg, kind))
            prepared.append((dg, kind, decision, reason, value))
    results = []
    cat, _ = open_catalog(catalog, secret or bench / "catalog.secret")
    with cat:
        for dg, kind, decision, reason, value in prepared:
            try:
                status = cat.decide(kind, value, DECISIONS[decision], reason=reason, source="manual")
            except (CatalogError, sqlite3.Error, OSError):
                raise SystemExit("refusing: catalog decision failed") from None
            results.append({"digest": dg, "kind": kind, "requested": DECISIONS[decision], "status": status})
    return results


# ------------------------------------------------------------------ CLI


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics echo rejected argument values verbatim.
        self.exit(2, "invalid command arguments; use --help for usage\n")


def main(argv: list[str] | None = None) -> int:
    ap = _PrivateArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for s in ("report", "decide"):
        p = sub.add_parser(s)
        p.add_argument("--bench", required=True)
        p.add_argument("--catalog", required=True, type=Path)
        p.add_argument("--secret", type=Path, help="default: <bench>/catalog.secret")
    r = sub.choices["report"]
    r.add_argument("--name", help="review dir name (default: timestamp)")
    r.add_argument("--show-private", action="store_true", help="print RAW values (protected terminal only)")
    d = sub.choices["decide"]
    d.add_argument("--report", required=True, type=Path, help="private-review.json from 'report'")
    d.add_argument("--digest")
    d.add_argument("--kind", choices=sorted(KINDS))
    d.add_argument("--decision", choices=list(DECISIONS))
    d.add_argument("--reason")
    d.add_argument("--stdin", action="store_true", help="read '<digest> <kind> <decision> <reason>' lines")
    a = ap.parse_args(argv)
    if a.cmd == "report":
        summary, private, out_dir = do_report(a.bench, a.catalog, a.secret, a.name)
        print(json.dumps(private if a.show_private else summary, indent=2, sort_keys=True))
        if a.show_private:
            print(f"review dir: {out_dir}", file=sys.stderr)
        return 0
    argv_mode = any(x is not None for x in (a.digest, a.kind, a.decision, a.reason))
    if a.stdin == argv_mode:
        ap.error("give either --stdin or all of --digest/--kind/--decision/--reason")
    if a.stdin:
        decisions = parse_decision_lines(sys.stdin.read())
    else:
        if None in (a.digest, a.kind, a.decision, a.reason):
            ap.error("--digest, --kind, --decision and --reason are all required")
        decisions = [(a.digest, a.kind, a.decision, a.reason)]
    if not decisions:
        ap.error("no decisions given")
    print(json.dumps(do_decide(a.bench, a.catalog, a.secret, a.report, decisions), indent=2))
    return 0


assert set(DECISIONS.values()) <= set(APPROVAL_STATUSES)

if __name__ == "__main__":
    raise SystemExit(main())
