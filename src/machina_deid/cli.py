"""Command line: ``python -m machina_deid <report|state|pdf|dxf|step|process|catalog|transfer> ...``.

No command here uploads, submits or deletes anything. Outputs must be written to
paths different from the source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import manifest as mf
from .classify import classify_path
from .idmap import IdentityMapError, load as load_idmap
from .state import StateDB, output_path_for

_TRANSFORMER_BY_FORMAT = {"pdf": "pdf", "dxf": "dxf", "step": "step"}


def _reserves(items: list[str]) -> dict[str, int]:
    out = {}
    for it in items:
        path, _, size = it.rpartition("=")
        if not path:
            raise SystemExit("--reserve expects PATH=SIZE (e.g. /srv/new2=20GiB)")
        out[path] = mf.parse_size(size)
    return out


def cmd_report(a) -> int:
    rows, bad = mf.parse_manifest(a.manifest)
    reserves = _reserves(a.reserve) or {"/": mf.parse_size("10GiB")}
    rep = mf.analyze(rows, bad, reserves=reserves, batch_bytes=mf.parse_size(a.batch_bytes),
                     output_ratio=a.output_ratio, archive_expansion=a.archive_expansion,
                     include_paths=a.include_paths)
    if a.out_dir:
        for p in mf.write_reports(rep, a.out_dir):
            print(p)
    else:
        sys.stdout.write(mf.to_markdown(rep))
    return 0


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def cmd_state(a) -> int:
    db = StateDB(a.db)
    try:
        if a.action == "ingest":
            rows, _ = mf.parse_manifest(a.manifest)
            root = Path(a.source_root).resolve()
            n = 0
            for r in rows[: a.limit] if a.limit else rows:
                phys = root / r.path
                cls = classify_path(phys) if phys.is_file() else None
                fmt = cls.format if cls and cls.status in ("verified", "unverified") else None
                n += db.register(r.path, r.path, phys, input_size=r.size,
                                 input_sha256=_sha(phys) if (a.checksum and phys.is_file()) else None,
                                 classification=f"{cls.format}/{cls.status}" if cls else "missing",
                                 transformer=_TRANSFORMER_BY_FORMAT.get(fmt))
            print(f"registered {n} new records")
        elif a.action == "status":
            print(json.dumps(db.counts(), indent=2))
        elif a.action == "recover":
            interrupted = db.recover_interrupted()
            transformed = db.recover_checkpoints()
            print(f"returned {interrupted} interrupted records to pending; "
                  f"reconciled {transformed} transformed checkpoints")
        elif a.action == "select":
            batch = db.select_batch(a.max_files, mf.parse_size(a.max_bytes) if a.max_bytes else None, a.transformer)
            for r in batch:
                print(json.dumps({"id": r["id"], "transformer": r["transformer"],
                                  "source_physical_path": r["source_physical_path"],
                                  "output_path": str(output_path_for(a.output_root, r["source_rel_path"]))}))
    finally:
        db.close()
    return 0


def _run_transform(kind: str, a) -> int:
    try:
        idmap = load_idmap(a.identity_map)
    except IdentityMapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if kind == "pdf":
        from .pdf_transform import transform_pdf
        res = transform_pdf(a.input, a.output, idmap, overwrite=a.overwrite)
    elif kind == "step":
        from .step_transform import transform_step
        res = transform_step(a.input, a.output, idmap, overwrite=a.overwrite)
    else:
        from .dxf_transform import transform_dxf
        res = transform_dxf(a.input, a.output, idmap, overwrite=a.overwrite)
    print(json.dumps(res.to_dict(), indent=2, sort_keys=True))
    return 0 if res.ok else 1


def cmd_process(a) -> int:
    from . import process as pr
    try:
        if a.dry_run:
            res = pr.plan(a.manifest, a.source_root, limit=a.limit, pdf_map=a.pdf_map, dxf_map=a.dxf_map,
                          step_map=a.step_map, archive_map=a.archive_map)
        else:
            res = pr.process(a.manifest, a.source_root, a.output_root, a.work_dir, limit=a.limit,
                             max_bytes=mf.parse_size(a.max_bytes), pdf_map=a.pdf_map, dxf_map=a.dxf_map,
                             step_map=a.step_map, archive_map=a.archive_map, link_mode=a.link_mode, retry_failed=a.retry_failed,
                             min_free=mf.parse_size(a.min_free))
    except (pr.ProcessError, IdentityMapError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


def cmd_transfer(a) -> int:
    import shlex

    from . import transfer as tr
    try:
        cmds = tr.build_commands(a.local_dir, a.dest, ssh_port=a.ssh_port, ssh_key=a.ssh_key,
                                 bwlimit_kbps=a.bwlimit_kbps)
    except tr.TransferError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for name, argv in cmds.items():
        print(f"# {name}\n{shlex.join(argv)}")
    if a.execute:
        return tr.run(cmds["transfer"])
    print("# not executed (pass --execute to run the transfer)")
    return 0


def cmd_readiness(a) -> int:
    from . import readiness as rd
    if a.action == "report":
        report = rd.new_report(a.manifest, a.output_root)
        if a.out:
            rd.write_checkpoint(report, a.out)
        print(json.dumps({"manifest_rows": report["manifest_rows"],
                          "malformed_manifest_lines": report["malformed_manifest_lines"],
                          "duplicate_manifest_paths": report["duplicate_manifest_paths"],
                          "duplicate_source_hashes": report["duplicate_source_hashes"],
                          "out": str(a.out) if a.out else None}, indent=2, sort_keys=True))
        return 0
    if a.action == "checkpoint":
        report = rd.read_checkpoint(a.checkpoint)
        results = json.loads(Path(a.results).read_text(encoding="utf-8"))
        if not isinstance(results, list):
            raise SystemExit("results must be a JSON list")
        print(json.dumps(rd.record_batch(report, results, a.checkpoint), indent=2, sort_keys=True))
        return 0
    if a.action == "validate":
        report = rd.read_checkpoint(a.report) if a.report else None
        result = rd.validate_final_layout(a.layout_root, a.manifest, report=report)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["valid"] else 1
    if a.action == "upload-plan":
        print(json.dumps(rd.upload_plan(a.layout_root, a.destination), indent=2, sort_keys=True))
        return 0
    raise SystemExit(f"unknown readiness action: {a.action}")


def _open_catalog(a):
    from .catalog import IdentityCatalog
    from .identity import load_secret
    return IdentityCatalog(a.catalog, load_secret(a.secret_file))


def cmd_catalog(a) -> int:
    """Two-pass workflow. Raw values are read from stdin (never argv) and never printed."""
    from . import twopass as tp
    from .catalog import CatalogError
    from .identity import IdentityError
    try:
        cat = _open_catalog(a)
    except (CatalogError, IdentityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        if a.action == "discover":
            if not a.paths:
                raise SystemExit("discover requires one or more paths")
            root = Path(a.source_root).resolve() if a.source_root else None
            items = []
            for p in a.paths:
                phys = Path(p).resolve()
                rel = str(phys.relative_to(root)) if root and phys.is_relative_to(root) else phys.name
                items.append((phys, rel))
            res = tp.discover_batch(cat, items, limit=a.limit,
                                    max_bytes=mf.parse_size(a.max_bytes) if a.max_bytes else None,
                                    auto=not a.no_auto)
            print(json.dumps(res.to_dict(), indent=2, sort_keys=True))
        elif a.action == "report":
            print(json.dumps(cat.report(), indent=2, sort_keys=True))
        elif a.action in ("approve", "reject", "review"):
            status = {"approve": "approved", "reject": "rejected", "review": "review"}[a.action]
            vals = [ln.rstrip("\r\n") for ln in sys.stdin if ln.strip()]
            out = {}
            for v in vals:
                r = cat.decide(a.kind, v, status, reason=a.reason or f"manual {a.action}")
                out[r] = out.get(r, 0) + 1
            print(json.dumps({"values": len(vals), "results": out}, sort_keys=True))
            return 0 if set(out) <= {status} else 1
        elif a.action == "alias":
            vals = [ln.rstrip("\r\n") for ln in sys.stdin if ln.strip()]
            if len(vals) != 2:
                raise SystemExit("alias expects exactly two stdin lines: canonical, alias")
            p = cat.approve_alias(a.kind, vals[0], vals[1], reason=a.reason or "manual alias approval")
            print(json.dumps({"pseudonym": p}))
        elif a.action == "transform":
            if len(a.paths) != 2:
                raise SystemExit("transform expects INPUT OUTPUT")
            res = tp.transform_file(cat, a.paths[0], a.paths[1], overwrite=a.overwrite)
            print(json.dumps(res.to_dict(), indent=2, sort_keys=True))
            return 0 if res.status in ("transformed", "no_approved_identities") else 1
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        cat.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="machina_deid")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="manifest storage/prioritization report")
    r.add_argument("manifest")
    r.add_argument("--out-dir", help="write manifest-report.json/.md here (e.g. work/)")
    r.add_argument("--reserve", action="append", default=[], metavar="PATH=SIZE")
    r.add_argument("--batch-bytes", default="5GiB")
    r.add_argument("--output-ratio", type=float, default=1.0)
    r.add_argument("--archive-expansion", type=float, default=3.0)
    r.add_argument("--include-paths", action="store_true", help="include raw paths (keep report private)")
    r.set_defaults(fn=cmd_report)

    s = sub.add_parser("state", help="processing state database")
    s.add_argument("action", choices=["ingest", "status", "recover", "select"])
    s.add_argument("--db", required=True)
    s.add_argument("--manifest")
    s.add_argument("--source-root", help="explicit physical root of the source files")
    s.add_argument("--limit", type=int)
    s.add_argument("--checksum", action="store_true")
    s.add_argument("--max-files", type=int, default=10)
    s.add_argument("--max-bytes")
    s.add_argument("--transformer")
    s.add_argument("--output-root", default="output")
    s.set_defaults(fn=cmd_state)

    for kind in ("pdf", "dxf", "step"):
        t = sub.add_parser(kind, help=f"transform one {kind.upper()} with an explicit identity map")
        t.add_argument("input")
        t.add_argument("output")
        t.add_argument("--identity-map", required=True)
        t.add_argument("--overwrite", action="store_true")
        t.set_defaults(fn=lambda a, k=kind: _run_transform(k, a))

    p = sub.add_parser("process", help="bounded, resumable, content-addressed batch processing")
    p.add_argument("--manifest", required=True)
    p.add_argument("--source-root", required=True, help="explicit physical root of the source files (read-only)")
    p.add_argument("--output-root", help="manifest-relative outputs go here (must not overlap source)")
    p.add_argument("--work-dir", help="state.sqlite + content-addressed store (must not overlap source/output)")
    p.add_argument("--limit", type=int, required=True, help="max manifest rows/records per run (hard cap 5000)")
    p.add_argument("--max-bytes", default="5GiB", help="max source bytes claimed per run")
    p.add_argument("--min-free", default="5GiB", help="free space to keep on the work filesystem")
    p.add_argument("--pdf-map")
    p.add_argument("--dxf-map")
    p.add_argument("--step-map", help="explicit identity map for STEP/STP FILE_NAME header strings")
    p.add_argument("--archive-map",
                   help="explicit identity map (same JSON strings format) applied to PDF/DXF/STEP members "
                        "inside .zip archives. ZIPs are always rebuilt and verified; without this map "
                        "supported members are copied unchanged and reported inspected_no_map (not "
                        "de-identified). Archives with residual identities fail; unsupported members "
                        "keep the record incomplete (unsupported_passthrough)")
    p.add_argument("--link-mode", choices=["auto", "copy"], default="auto")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="classify/hash and report the plan; write nothing")
    p.set_defaults(fn=cmd_process)

    c = sub.add_parser("catalog", help="two-pass workflow: discover -> report/approve -> transform")
    c.add_argument("action", choices=["discover", "report", "approve", "reject", "review", "alias", "transform"])
    c.add_argument("paths", nargs="*", help="discover: files; transform: INPUT OUTPUT")
    c.add_argument("--catalog", required=True, help="catalog sqlite path (private)")
    c.add_argument("--secret-file", help="HMAC secret file (else $MACHINA_DEID_SECRET)")
    c.add_argument("--source-root", help="discover: make recorded paths relative to this root")
    c.add_argument("--limit", type=int, default=100, help="discover: max files (hard cap 5000)")
    c.add_argument("--max-bytes", help="discover: max source bytes")
    c.add_argument("--no-auto", action="store_true", help="discover: record candidates only")
    c.add_argument("--kind", default="text", help="approve/reject/review/alias identity kind")
    c.add_argument("--reason", help="short ASCII reason (must not contain the value)")
    c.add_argument("--overwrite", action="store_true")
    c.set_defaults(fn=cmd_catalog)

    x = sub.add_parser("transfer", help="print (or with --execute run) a resumable rsync/SSH push")
    x.add_argument("--local-dir", required=True)
    x.add_argument("--dest", required=True, help="[user@]host:/absolute/path")
    x.add_argument("--ssh-port", type=int)
    x.add_argument("--ssh-key")
    x.add_argument("--bwlimit-kbps", type=int)
    x.add_argument("--execute", action="store_true")
    x.set_defaults(fn=cmd_transfer)

    z = sub.add_parser("readiness", help="storage-independent full-corpus accounting and layout checks")
    z.add_argument("action", choices=["report", "checkpoint", "validate", "upload-plan"])
    z.add_argument("--manifest", help="full corpus manifest")
    z.add_argument("--output-root", default="corpus", help="manifest-relative output root in the report")
    z.add_argument("--out", help="write report/checkpoint JSON here")
    z.add_argument("--checkpoint", help="existing checkpoint JSON")
    z.add_argument("--results", help="JSON list of terminal row results")
    z.add_argument("--layout-root", help="submission layout root")
    z.add_argument("--report", help="checkpoint/report JSON for recorded output hashes")
    z.add_argument("--destination", help="gs:// destination for upload-plan")
    z.set_defaults(fn=cmd_readiness)

    a = ap.parse_args(argv)
    if a.cmd == "process" and not a.dry_run and not (a.output_root and a.work_dir):
        ap.error("process requires --output-root and --work-dir unless --dry-run")
    if a.cmd == "state" and a.action == "ingest" and not (a.manifest and a.source_root):
        ap.error("ingest requires --manifest and --source-root")
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
