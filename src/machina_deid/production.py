"""Resumable, parallel full-corpus production runner (local WSL attempt assembly).

Compatible with the serial reference runner's on-disk state::

    <root>/input/.get-input/manifest.tsv      official manifest (read only)
    <root>/input/<manifest path>              raw corpus (never written)
    <root>/repo                               clean checkout of the pinned revision
    <root>/ops/download.exit                  downloader exit status (written by the downloader)
    <root>/ops/attempt-1/{ledger.sqlite,catalog.sqlite,catalog.pseudonyms.json,seed,revision}
    <root>/attempts/attempt-1/{corpus,code,README.txt}

Phases
------
1. Discovery (ordered, parallel parse): size-matched available files are parsed in bounded batches by
   worker processes that never touch the catalog; the parent records results strictly in manifest
   order, one ``discovery`` ledger row per file, so first-decision-wins approvals equal a serial run
   over the same availability. Repeats until every manifest row is discovered (catches up while the
   download is still running).
2. Freeze: once discovery is complete the catalog is checkpointed, switched to rollback journal and
   its digest is stored in the ledger. Any later change to the catalog aborts the run.
3. Transform (parallel): workers open the frozen catalog read only, write one staging file each under
   ``ops/attempt-1/staging`` (same filesystem), validate streamed hashes and return. Only the parent
   renames staging files into the corpus, fsyncs, and commits ``output`` ledger rows.
4. Package: coverage, integrity (streamed hashes), organizer limits, ``code/`` and ``README.txt``.

Logs and summaries contain counts and status codes only: never paths, values or exception text.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import manifest, twopass

OFFICIAL_ROWS = 188_722
MAX_FILE_BYTES = 2_000_000_000  # organizer per-file cap (decimal 2 GB)
MAX_CORPUS_OBJECTS = 400_000
CHUNK = 1 << 20
# The only revision whose live state may be adopted (discovery semantics are unchanged since then).
MIGRATABLE_FROM = {
    "dc3da69225167cfe81bfbd5803ed02902efd0219",
    # 27757e3: identical on-disk state and discovery semantics; only per-row commits differ.
    "27757e305f9dbc8e4b607f991a6491734a7091ed",
}
RUNNER_PATTERNS = ("run_full_corpus", "local-runner")
TEST_CRASH_ENV = "MACHINA_DEID_TEST_CRASH_SUFFIX"  # test-only: worker exits hard on this suffix

LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS discovery (path TEXT PRIMARY KEY, status TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS output (path TEXT PRIMARY KEY, status TEXT NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL)",
)
EXTRA_SCHEMA = (
    # Additive tables; the reference runner ignores them.
    "CREATE TABLE IF NOT EXISTS output_detail (path TEXT PRIMARY KEY, reason TEXT NOT NULL, "
    "residual_approved INTEGER NOT NULL, fields_replaced INTEGER NOT NULL, fully_clean INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS runner_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


class RunnerError(RuntimeError):
    """Operational failure. Messages never contain paths, identities or secrets."""


# ------------------------------------------------------------------ configuration


@dataclass
class Config:
    root: Path = Path("/mnt/machina-deid")
    attempt: str = "attempt-1"
    expected_rows: int = OFFICIAL_ROWS
    expected_revision: str = ""
    workers: int = max(1, min(12, (os.cpu_count() or 2) - 2))
    discovery_workers: int = 0  # 0 -> workers
    batch_files: int = 256
    batch_bytes: int = 4 * 1024**3
    heavy_bytes: int = 256 * 1024**2
    heavy_slots: int = 2
    tasks_per_child: int = 200
    reserve_bytes: int = 15 * 1024**3
    poll_seconds: float = 15.0
    verify_md5: bool = True
    check_git: bool = True
    stop_after_outputs: int = 0  # testing/interruption drills: stop cleanly after N new outputs
    commit_every: int = 32

    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def ops(self) -> Path:
        return self.root / "ops"

    @property
    def private(self) -> Path:
        return self.ops / self.attempt

    @property
    def attempt_dir(self) -> Path:
        return self.root / "attempts" / self.attempt

    @property
    def corpus(self) -> Path:
        return self.attempt_dir / "corpus"

    @property
    def staging(self) -> Path:
        return self.private / "staging"

    @property
    def manifest_path(self) -> Path:
        return self.input / ".get-input" / "manifest.tsv"

    @property
    def catalog_path(self) -> Path:
        return self.private / "catalog.sqlite"

    @property
    def store_path(self) -> Path:
        return self.catalog_path.with_suffix(".pseudonyms.json")


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg, flush=True)


# ------------------------------------------------------------------ small helpers


def hash_file(path: Path, *, md5: bool = False) -> tuple[str, str]:
    """Streamed SHA-256 (and optional MD5) in a single read."""
    sha, m = hashlib.sha256(), hashlib.md5(usedforsecurity=False) if md5 else None
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            sha.update(chunk)
            if m is not None:
                m.update(chunk)
    return sha.hexdigest(), (m.hexdigest() if m is not None else "")


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp.open("w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def verified_input(input_root: Path, rel: str, size: int) -> Path | None:
    """Same rule as the reference runner: exact non-symlink regular file with the manifest size."""
    source = input_root / rel
    try:
        actual = source.resolve(strict=True)
        if actual != source or not actual.is_file() or actual.stat().st_size != size:
            return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    return actual


def require_space(cfg: Config) -> None:
    if shutil.disk_usage(cfg.root).free < cfg.reserve_bytes:
        raise RunnerError("workspace is below the free-space reserve")


def is_heavy(cfg: Config, row: manifest.Row) -> bool:
    return row.size >= cfg.heavy_bytes or PurePosixPath(row.path).suffix.lower() == ".zip"


def pool(workers: int, cfg: Config, initializer=None, initargs=()) -> ProcessPoolExecutor:
    # forkserver: children never inherit the parent's open SQLite handles.
    ctx = multiprocessing.get_context("forkserver" if sys.platform.startswith("linux") else "spawn")
    return ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=initializer, initargs=initargs,
                               max_tasks_per_child=cfg.tasks_per_child)


class RunLock:
    """Exclusive, non-blocking advisory lock for one attempt's private state."""

    def __init__(self, cfg: Config):
        cfg.private.mkdir(parents=True, exist_ok=True)
        self._fh = (cfg.private / "runner.lock").open("a")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            raise RunnerError("another runner holds the attempt lock") from None

    def close(self) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def other_runner_processes(patterns=RUNNER_PATTERNS) -> int:
    """Count processes (other than this one and its ancestors) whose command line names a runner."""
    skip, pid = set(), os.getpid()
    while pid > 1:
        skip.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    count = 0
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else ():
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if any(p in cmd for p in patterns) and "pytest" not in cmd:
            count += 1
    return count


# ------------------------------------------------------------------ state checks


def git_head(cfg: Config) -> str:
    commit = subprocess.check_output(["git", "-C", str(cfg.repo), "rev-parse", "HEAD"], text=True).strip()
    if subprocess.check_output(["git", "-C", str(cfg.repo), "status", "--porcelain"], text=True).strip():
        raise RunnerError("repository checkout must be clean")
    return commit


def load_rows(cfg: Config) -> list[manifest.Row]:
    rows, malformed = manifest.parse_manifest(cfg.manifest_path)
    if malformed or len(rows) != cfg.expected_rows or len({r.path for r in rows}) != len(rows):
        raise RunnerError("unexpected manifest coverage")
    for r in rows:
        p = PurePosixPath(r.path)
        if p.is_absolute() or ".." in p.parts or not r.path or r.size < 0:
            raise RunnerError("unsafe path in the manifest")
    return rows


def open_ledger(cfg: Config) -> sqlite3.Connection:
    ledger = sqlite3.connect(cfg.private / "ledger.sqlite")
    ledger.execute("PRAGMA synchronous = FULL")
    for stmt in LEDGER_SCHEMA + EXTRA_SCHEMA:
        ledger.execute(stmt)
    ledger.commit()
    return ledger


def ensure_seed(cfg: Config) -> Path:
    seed = cfg.private / "seed"
    if not seed.exists():
        with seed.open("xb") as fh:
            fh.write(os.urandom(32))
            fh.flush()
            os.fsync(fh.fileno())
        fsync_dir(cfg.private)
    return seed


def check_revision(cfg: Config) -> str:
    """Pin/verify the revision. A run pinned to another revision must be migrated explicitly."""
    commit = git_head(cfg) if cfg.check_git else cfg.expected_revision
    if cfg.expected_revision and commit != cfg.expected_revision:
        raise RunnerError("checkout does not match the expected revision")
    pinned = cfg.private / "revision"
    if pinned.exists():
        if pinned.read_text().strip() != commit:
            raise RunnerError("run is pinned to a different revision; use the explicit migrate command")
    else:
        atomic_write(pinned, commit + "\n")
    return commit


def meta_get(ledger, key):
    row = ledger.execute("SELECT value FROM runner_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------ discovery


def _discover_task(src: str, rel: str):
    """Worker: parse one file. No catalog access; returns raw candidates in memory only."""
    path = Path(src)
    if twopass.detect_format(path) is None:
        return "unsupported_format", []
    try:
        return "parsed", twopass.discover_file(path, rel)
    except Exception:  # noqa: BLE001 - recorded as an explicit status, never as text
        return "discovery_exception", []


def _batches(cfg: Config, ready):
    batch, used, heavy = [], 0, 0
    for item in ready:
        row = item[0]
        h = is_heavy(cfg, row)
        if batch and (len(batch) >= cfg.batch_files or used + row.size > cfg.batch_bytes
                      or (h and heavy >= cfg.heavy_slots)):
            yield batch
            batch, used, heavy = [], 0, 0
        batch.append(item)
        used += row.size
        heavy += h
    if batch:
        yield batch


def discover(cfg: Config, rows, ledger, catalog) -> None:
    done = {p for (p,) in ledger.execute("SELECT path FROM discovery")}
    total = len(rows)
    workers = cfg.discovery_workers or cfg.workers
    log(f"Discovery resume: {len(done):,}/{total:,} recorded; workers={workers}")
    last_report = len(done)
    executor = pool(workers, cfg) if workers > 1 else None
    try:
        exit_seen = False
        while len(done) < total:
            ready = []
            for r in rows:
                if r.path not in done and (src := verified_input(cfg.input, r.path, r.size)) is not None:
                    ready.append((r, src))
            for batch in _batches(cfg, ready):
                require_space(cfg)
                args = [(str(src), r.path) for r, src in batch]
                results = (executor.map(_discover_task, *zip(*args)) if executor
                           else (_discover_task(*a) for a in args))
                # Group commit (one durable transaction per batch, see docs/production-runner.md):
                # pseudonym store fsync -> catalog COMMIT -> ledger COMMIT. A crash at any point
                # leaves ledger rows only for catalog effects that are already durable; uncommitted
                # rows are simply re-discovered (all catalog writes are idempotent).
                staged = []
                with catalog.group():
                    for (r, _), (kind, members) in zip(batch, results):  # strictly manifest order
                        if kind == "parsed":
                            res = twopass.BatchResult()
                            twopass._record_discovered(catalog, [members], res, True)
                            status = "discovery_exception" if res.failed else "inspected"
                        else:
                            status = kind
                        staged.append((r.path, status))
                if os.environ.get(TEST_CRASH_ENV + "_DISCOVERY") == "after_catalog":
                    os._exit(71)  # test-only crash window between catalog and ledger commits
                ledger.executemany("INSERT INTO discovery VALUES (?, ?)", staged)
                ledger.commit()
                done.update(p for p, _ in staged)
                if len(done) - last_report >= 250 or len(done) == total:
                    log(f"Discovery checkpoint: {len(done):,}/{total:,}")
                    last_report = len(done)
            if len(done) == total:
                break
            exit_file = cfg.ops / "download.exit"
            if exit_file.exists() and exit_file.read_text().strip():
                if exit_seen and not ready:
                    raise RunnerError(f"downloader exited with status {exit_file.read_text().strip()[:16]!r}; "
                                      f"{total - len(done):,} rows await complete input")
                exit_seen = True  # one more sweep for files that completed just before the exit marker
                continue
            time.sleep(cfg.poll_seconds)
    finally:
        if executor:
            executor.shutdown(cancel_futures=True)


def discovery_counts(ledger) -> dict:
    return dict(ledger.execute("SELECT status, COUNT(*) FROM discovery GROUP BY status").fetchall())


# ------------------------------------------------------------------ freeze


def catalog_digest(cfg: Config) -> str:
    wal = cfg.catalog_path.with_name(cfg.catalog_path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise RunnerError("catalog has an unflushed write-ahead log; it is not frozen")
    h = hashlib.sha256()
    for p in (cfg.catalog_path, cfg.store_path):
        h.update(p.name.encode() + b"\0")
        h.update(hash_file(p)[0].encode() if p.exists() else b"absent")
    return h.hexdigest()


def freeze_catalog(cfg: Config, ledger) -> str:
    """Make the catalog a read-only artefact for the transform phase (idempotent)."""
    frozen = meta_get(ledger, "catalog_digest")
    if frozen is not None:
        if catalog_digest(cfg) != frozen:
            raise RunnerError("catalog changed after it was frozen for transformation")
        return frozen
    db = sqlite3.connect(cfg.catalog_path)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode = DELETE")
    finally:
        db.close()
    digest = catalog_digest(cfg)
    ledger.execute("INSERT INTO runner_meta VALUES ('catalog_digest', ?)", (digest,))
    ledger.commit()
    return digest


# ------------------------------------------------------------------ transform

_CAT = None


def _init_transform_worker(catalog_path: str, seed_path: str) -> None:
    global _CAT
    from .catalog import IdentityCatalog
    _CAT = IdentityCatalog(catalog_path, Path(seed_path).read_bytes(), read_only=True)


def _stage_name(rel: str) -> str:
    return "s-" + hashlib.sha256(rel.encode("utf-8", "surrogateescape")).hexdigest()[:40] + \
        PurePosixPath(rel).suffix.lower()[:16]


def _transform_task(input_root: str, staging: str, rel: str, size: int, md5: str, verify_md5: bool,
                    crash: str = "") -> dict:
    """Worker: produce one staged, fsynced output. Never writes outside ``staging``."""
    src = verified_input(Path(input_root), rel, size)
    if src is None:
        return {"error": "input_changed_after_discovery"}
    if crash and rel.endswith(crash):
        os._exit(70)
    before, got_md5 = hash_file(src, md5=verify_md5 and bool(md5))
    if verify_md5 and md5 and got_md5 != md5:
        return {"error": "input_md5_mismatch"}
    stage = Path(staging) / _stage_name(rel)
    stage.unlink(missing_ok=True)
    detail = {"reason": "", "residual_approved": 0, "fields_replaced": 0, "fully_clean": 0}
    if twopass.detect_format(src):
        try:
            res = twopass.transform_file(_CAT, src, stage, rel)
            status = res.status
            detail.update(reason=res.reason[:200], residual_approved=res.residual_approved,
                          fields_replaced=res.fields_replaced, fully_clean=int(res.fully_clean))
            if not res.source_unchanged:
                stage.unlink(missing_ok=True)
                return {"error": "source_changed_during_transform"}
        except Exception as exc:  # noqa: BLE001 - explicit failed status; type name only
            status, detail["reason"] = "failed", f"transform_exception:{type(exc).__name__}"
    else:
        status = "unsupported_passthrough"
    if status != "transformed":
        stage.unlink(missing_ok=True)
        shutil.copyfile(src, stage)
    digest = hash_file(stage)[0]
    if status != "transformed" and digest != before:
        stage.unlink(missing_ok=True)
        return {"error": "passthrough_not_byte_identical"}
    fsync_file(stage)
    return {"status": status, "size": stage.stat().st_size, "digest": digest, "stage": str(stage), **detail}


def _passthrough_in_parent(cfg: Config, row) -> dict:
    """Last resort after a worker crashed twice on this file: byte-identical copy, status failed."""
    src = verified_input(cfg.input, row.path, row.size)
    if src is None:
        return {"error": "input_changed_after_discovery"}
    before = hash_file(src)[0]
    stage = cfg.staging / _stage_name(row.path)
    stage.unlink(missing_ok=True)
    shutil.copyfile(src, stage)
    digest = hash_file(stage)[0]
    if digest != before:
        stage.unlink(missing_ok=True)
        return {"error": "passthrough_not_byte_identical"}
    fsync_file(stage)
    return {"status": "failed", "size": stage.stat().st_size, "digest": digest, "stage": str(stage),
            "reason": "worker_crash", "residual_approved": 0, "fields_replaced": 0, "fully_clean": 0}


class _Committer:
    def __init__(self, cfg: Config, ledger):
        self.cfg, self.ledger, self.pending, self.new = cfg, ledger, 0, 0
        self.made_dirs: set[Path] = set()

    def commit_result(self, row, r: dict) -> None:
        output = self.cfg.corpus / row.path
        if output.parent not in self.made_dirs:
            output.parent.mkdir(parents=True, exist_ok=True)
            self.made_dirs.add(output.parent)
        os.replace(r["stage"], output)
        fsync_dir(output.parent)
        self.ledger.execute("INSERT INTO output VALUES (?, ?, ?, ?)", (row.path, r["status"], r["size"], r["digest"]))
        self.ledger.execute("INSERT OR REPLACE INTO output_detail VALUES (?, ?, ?, ?, ?)",
                            (row.path, r["reason"], r["residual_approved"], r["fields_replaced"], r["fully_clean"]))
        self.pending += 1
        self.new += 1
        if self.pending >= self.cfg.commit_every:
            self.flush()

    def flush(self) -> None:
        self.ledger.commit()
        self.pending = 0


class _Stop(Exception):
    pass


def _run_pool(cfg: Config, todo, committer: _Committer, errors: Counter, workers: int):
    """Bounded, heavy-aware submission.

    Returns None when ``todo`` is exhausted, else ``(suspects, rest)`` after the pool broke. The pool can
    break at any moment (a worker dies), so BrokenProcessPool is handled both at ``submit`` and at
    ``result``. Every input row ends up exactly once in: committed, an error count, ``suspects`` (was
    submitted and may have killed a worker), or ``rest`` (never submitted). Futures that completed
    successfully before the break are still committed, never re-run.
    """
    args0 = (str(cfg.input), str(cfg.staging))
    crash = os.environ.get(TEST_CRASH_ENV, "")
    seed = str(cfg.private / "seed")
    ex = pool(workers, cfg, _init_transform_worker, (str(cfg.catalog_path), seed))
    inflight: dict = {}
    heavy_now, it, nxt = 0, iter(todo), None

    def handle(row, fut) -> bool:
        """Commit/record one finished future. False if it died with the pool."""
        try:
            r = fut.result()
        except BrokenProcessPool:
            return False
        if "error" in r:
            errors[r["error"]] += 1
            return True
        committer.commit_result(row, r)
        if committer.new % 500 == 0:
            log(f"Output checkpoint: +{committer.new:,} this session")
        if cfg.stop_after_outputs and committer.new >= cfg.stop_after_outputs:
            raise _Stop
        return True

    def broken():
        lost = []
        for fut, (row, _) in list(inflight.items()):
            del inflight[fut]
            if not (fut.done() and handle(row, fut)):
                lost.append(row)
        rest = ([] if nxt is None else [nxt]) + list(it)
        if not lost and rest:
            # Pool broke with nothing in flight (e.g. died at startup). Isolate the next row so the
            # caller always makes progress instead of looping on a fresh pool forever.
            lost, rest = rest[:1], rest[1:]
        return lost, rest

    try:
        while True:
            while len(inflight) < workers * 2:
                if nxt is None:
                    nxt = next(it, None)
                    if nxt is None:
                        break
                h = is_heavy(cfg, nxt)
                if h and heavy_now >= cfg.heavy_slots:
                    break
                require_space(cfg)
                try:
                    fut = ex.submit(_transform_task, *args0, nxt.path, nxt.size, nxt.md5, cfg.verify_md5, crash)
                except BrokenProcessPool:
                    return broken()  # nxt was not submitted: it stays in ``rest``
                inflight[fut] = (nxt, h)
                heavy_now += h
                nxt = None
            if not inflight:
                return None
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in finished:
                row, h = inflight[fut]
                if not handle(row, fut):
                    return broken()
                del inflight[fut]
                heavy_now -= h
    finally:
        ex.shutdown(cancel_futures=True)


def assemble(cfg: Config, rows, ledger) -> Counter:
    done = {p for (p,) in ledger.execute("SELECT path FROM output")}
    todo = [r for r in rows if r.path not in done]
    if cfg.staging.exists():
        shutil.rmtree(cfg.staging)  # only our own staging files; never corpus or input
    cfg.staging.mkdir(parents=True)
    cfg.corpus.mkdir(parents=True, exist_ok=True)
    if os.stat(cfg.staging).st_dev != os.stat(cfg.corpus).st_dev:
        raise RunnerError("staging and corpus must be on the same filesystem")
    log(f"Transform resume: {len(done):,}/{len(rows):,} outputs recorded; workers={cfg.workers}")
    committer, errors = _Committer(cfg, ledger), Counter()
    try:
        queue = todo
        while queue:
            ret = _run_pool(cfg, queue, committer, errors, cfg.workers)
            if ret is None:
                break
            suspects, queue = ret
            log(f"Worker process died; retrying {len(suspects)} in-flight files one at a time")
            committer.flush()
            for row in suspects:  # isolate the culprit in a single-worker pool
                if _run_pool(cfg, [row], committer, errors, 1) is not None:
                    r = _passthrough_in_parent(cfg, row)
                    if "error" in r:
                        errors[r["error"]] += 1
                    else:
                        committer.commit_result(row, r)
    except _Stop:
        log("Stopping early as requested (resumable)")
    finally:
        committer.flush()
    return errors


# ------------------------------------------------------------------ package


def _check_one(corpus: Path, rec):
    path, status, size, digest = rec
    artifact = corpus / path
    st = artifact.stat()
    return st.st_size == size and hash_file(artifact)[0] == digest


def summarize(cfg: Config, ledger) -> dict:
    outcomes = dict(ledger.execute("SELECT status, COUNT(*) FROM output GROUP BY status").fetchall())
    q = lambda sql: ledger.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "outcomes": outcomes,
        "discovery": discovery_counts(ledger),
        "transformed_with_residual": q("SELECT COUNT(*) FROM output o JOIN output_detail d USING(path) "
                                       "WHERE o.status='transformed' AND d.residual_approved>0"),
        "transformed_fully_clean": q("SELECT COUNT(*) FROM output o JOIN output_detail d USING(path) "
                                     "WHERE o.status='transformed' AND d.fully_clean=1"),
        "failed_reasons": dict(ledger.execute(
            "SELECT d.reason, COUNT(*) FROM output o JOIN output_detail d USING(path) WHERE o.status='failed' "
            "GROUP BY d.reason ORDER BY 2 DESC LIMIT 50").fetchall()),
        "files_over_2gb": q(f"SELECT COUNT(*) FROM output WHERE size > {MAX_FILE_BYTES}"),
        "output_objects": q("SELECT COUNT(*) FROM output"),
        "output_bytes": q("SELECT COALESCE(SUM(size),0) FROM output"),
    }


README = """Machina CAD De-ID

Use Ubuntu WSL with an ext4 workspace (default /mnt/machina-deid; any root with the same
input/repo/ops/attempts layout works). Check out the commit below into repo, retrieve the official
corpus into input, install the checkout with Python 3.12 and the recorded dependency versions, and
run code/run_full_corpus.py run --expected-revision <commit>. The identity seed is generated locally
and retained privately; it is not part of this package.

Only identities approved by the pinned pipeline are transformed. Unsupported files, files with no
approved identities, and files whose transforms fail are retained byte-for-byte. These files are NOT
de-identified and may still contain identities. Transformed files may also retain residual approved
values (counted below). Filenames are preserved.

"""


def package(cfg: Config, rows, ledger, commit: str, script: Path | None) -> dict:
    expected = {r.path for r in rows}
    actual = {p.relative_to(cfg.corpus).as_posix() for p in cfg.corpus.rglob("*") if p.is_file() and not p.is_symlink()}
    recorded = {p for (p,) in ledger.execute("SELECT path FROM output")}
    if actual != expected or recorded != expected:
        raise RunnerError(f"output coverage mismatch: {len(expected - actual)} missing, {len(actual - expected)} extra, "
                          f"{len(expected - recorded)} unrecorded")
    recs = ledger.execute("SELECT path, status, size, digest FROM output").fetchall()
    with ThreadPoolExecutor(max(1, cfg.workers)) as tp:  # hashlib releases the GIL
        bad = sum(not ok for ok in tp.map(lambda rec: _check_one(cfg.corpus, rec), recs, chunksize=64))
    if bad:
        raise RunnerError(f"final output integrity check failed for {bad} files")
    summary = {"commit": commit, "manifest_files": len(expected), **summarize(cfg, ledger)}
    summary["upload_blocked_by_limits"] = bool(summary["files_over_2gb"] or len(expected) > MAX_CORPUS_OBJECTS)
    code = cfg.attempt_dir / "code"
    if code.exists():
        shutil.rmtree(code)
    code.mkdir()
    if cfg.check_git:
        with subprocess.Popen(["git", "-C", str(cfg.repo), "archive", commit], stdout=subprocess.PIPE) as archive:
            subprocess.run(["tar", "-xf", "-", "-C", str(code)], stdin=archive.stdout, check=True)
            if archive.wait():
                raise RunnerError("git source archive failed")
    if script is not None:
        shutil.copyfile(script, code / "run_full_corpus.py")
    with (code / "run-dependencies.txt").open("w") as target:
        subprocess.run([sys.executable, "-m", "pip", "freeze", "--exclude-editable"], stdout=target, check=False,
                       stderr=subprocess.DEVNULL)
    atomic_write(cfg.attempt_dir / "README.txt", README + json.dumps(summary, indent=2) + "\n")
    return summary


# ------------------------------------------------------------------ entry points


def run(cfg: Config, *, script: Path | None = None, discovery_only: bool = False, package_output: bool = True) -> dict:
    os.umask(0o077)
    cfg.private.mkdir(parents=True, exist_ok=True)
    with RunLock(cfg):
        commit = check_revision(cfg)
        rows = load_rows(cfg)
        seed = ensure_seed(cfg)
        ledger = open_ledger(cfg)
        try:
            frozen = meta_get(ledger, "catalog_digest")
            n_disc = ledger.execute("SELECT COUNT(*) FROM discovery").fetchone()[0]
            if frozen is None and n_disc < len(rows):
                from .catalog import IdentityCatalog
                log(f"Discovery started for {len(rows):,} manifest rows at {commit[:7]}")
                catalog = IdentityCatalog(cfg.catalog_path, seed.read_bytes())
                try:
                    discover(cfg, rows, ledger, catalog)
                finally:
                    catalog.close()
            if discovery_only:
                return {"phase": "discovery", "discovery": discovery_counts(ledger)}
            if ledger.execute("SELECT COUNT(*) FROM discovery").fetchone()[0] != len(rows):
                raise RunnerError("discovery is incomplete; transformation needs the complete catalog")
            freeze_catalog(cfg, ledger)
            errors = assemble(cfg, rows, ledger)
            freeze_catalog(cfg, ledger)  # re-verify nothing touched the catalog
            remaining = len(rows) - ledger.execute("SELECT COUNT(*) FROM output").fetchone()[0]
            if errors:
                log("Transform errors (not recorded, will retry on resume): " + json.dumps(dict(errors)))
            if remaining:
                return {"phase": "transform", "remaining": remaining, "errors": dict(errors),
                        **summarize(cfg, ledger)}
            if not package_output:
                return {"phase": "transform", "remaining": 0, **summarize(cfg, ledger)}
            summary = package(cfg, rows, ledger, commit, script)
            log(json.dumps(summary))
            return {"phase": "packaged", **summary}
        finally:
            ledger.close()


def migrate(cfg: Config, from_revision: str, to_revision: str) -> dict:
    """Adopt a dc3da69 or 27757e3 discovery-phase state for this revision; refuses once any output exists."""
    if from_revision not in MIGRATABLE_FROM:
        raise RunnerError("only the documented dc3da69 or 27757e3 state can be migrated")
    if len(to_revision) != 40:
        raise RunnerError("target revision must be a full 40-character commit id")
    if other_runner_processes():
        raise RunnerError("a runner process is still running; stop it first")
    with RunLock(cfg):
        if cfg.check_git and git_head(cfg) != to_revision:
            raise RunnerError("checkout does not match the target revision")
        pinned = cfg.private / "revision"
        current = pinned.read_text().strip() if pinned.exists() else ""
        if current == to_revision:
            return {"migrated": False, "already": True}
        if current != from_revision:
            raise RunnerError("pinned revision is not the migratable source revision")
        for name in ("ledger.sqlite", "catalog.sqlite", "seed"):
            if not (cfg.private / name).exists():
                raise RunnerError("private state is incomplete")
        ledger = sqlite3.connect(cfg.private / "ledger.sqlite")
        try:
            if ledger.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RunnerError("ledger integrity check failed")
            tables = {t for (t,) in ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "output" in tables and ledger.execute("SELECT COUNT(*) FROM output").fetchone()[0]:
                raise RunnerError("outputs are already committed; migration is only allowed before output")
            n_disc = ledger.execute("SELECT COUNT(*) FROM discovery").fetchone()[0] if "discovery" in tables else 0
        finally:
            ledger.close()
        if cfg.corpus.exists() and any(p.is_file() for p in cfg.corpus.rglob("*")):
            raise RunnerError("corpus already contains files; migration is only allowed before output")
        from .catalog import IdentityCatalog
        IdentityCatalog(cfg.catalog_path, (cfg.private / "seed").read_bytes(), read_only=True).close()
        backup = cfg.private / f"pre-migration-{from_revision[:7]}"
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir()
        for name in ("ledger.sqlite", "catalog.sqlite"):
            src, dst = sqlite3.connect(cfg.private / name), sqlite3.connect(backup / name)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        if cfg.store_path.exists():
            shutil.copy2(cfg.store_path, backup / cfg.store_path.name)
        atomic_write(backup / "revision", from_revision + "\n")
        record = {"from": from_revision, "to": to_revision, "discovery_rows": n_disc,
                  "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        atomic_write(cfg.private / "migration.json", json.dumps(record, indent=2) + "\n")
        atomic_write(pinned, to_revision + "\n")
        return {"migrated": True, **record}


def rollback(cfg: Config) -> dict:
    """Re-pin the source revision recorded by :func:`migrate`, only while no output exists."""
    if other_runner_processes():
        raise RunnerError("a runner process is still running; stop it first")
    with RunLock(cfg):
        record = json.loads((cfg.private / "migration.json").read_text())
        ledger = sqlite3.connect(cfg.private / "ledger.sqlite")
        try:
            tables = {t for (t,) in ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "output" in tables and ledger.execute("SELECT COUNT(*) FROM output").fetchone()[0]:
                raise RunnerError("outputs exist; roll back manually per the runbook")
            if "runner_meta" in tables:
                ledger.execute("DELETE FROM runner_meta WHERE key='catalog_digest'")
                ledger.commit()
        finally:
            ledger.close()
        atomic_write(cfg.private / "revision", record["from"] + "\n")
        return {"rolled_back_to": record["from"]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=("run", "discover", "status", "migrate", "rollback-migration"))
    p.add_argument("--root", type=Path, default=Config.root)
    p.add_argument("--attempt", default="attempt-1")
    p.add_argument("--expected-rows", type=int, default=OFFICIAL_ROWS)
    p.add_argument("--expected-revision", default="")
    p.add_argument("--from-revision", default="")
    p.add_argument("--workers", type=int, default=Config.workers)
    p.add_argument("--discovery-workers", type=int, default=0)
    p.add_argument("--batch-files", type=int, default=256)
    p.add_argument("--batch-bytes", type=int, default=4 * 1024**3)
    p.add_argument("--heavy-bytes", type=int, default=256 * 1024**2)
    p.add_argument("--heavy-slots", type=int, default=2)
    p.add_argument("--reserve-bytes", type=int, default=15 * 1024**3)
    p.add_argument("--poll-seconds", type=float, default=15.0)
    p.add_argument("--no-verify-md5", action="store_true")
    p.add_argument("--no-package", action="store_true")
    p.add_argument("--stop-after-outputs", type=int, default=0)
    return p


def main(argv=None, script: Path | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.workers < 1 or a.workers > 64 or a.heavy_slots < 1 or a.batch_files < 1:
        raise SystemExit("workers must be 1..64; heavy-slots and batch-files must be >= 1")
    cfg = Config(root=a.root, attempt=a.attempt, expected_rows=a.expected_rows, expected_revision=a.expected_revision,
                 workers=a.workers, discovery_workers=a.discovery_workers, batch_files=a.batch_files,
                 batch_bytes=a.batch_bytes, heavy_bytes=a.heavy_bytes, heavy_slots=a.heavy_slots,
                 reserve_bytes=a.reserve_bytes, poll_seconds=a.poll_seconds, verify_md5=not a.no_verify_md5,
                 stop_after_outputs=a.stop_after_outputs)
    try:
        if a.command in ("run", "discover"):
            if not a.expected_revision:
                raise RunnerError("--expected-revision is required")
            out = run(cfg, script=script, discovery_only=a.command == "discover", package_output=not a.no_package)
        elif a.command == "migrate":
            out = migrate(cfg, a.from_revision, a.expected_revision)
        elif a.command == "rollback-migration":
            out = rollback(cfg)
        else:
            ledger = sqlite3.connect(f"{(cfg.private / 'ledger.sqlite').resolve().as_uri()}?mode=ro", uri=True)
            try:
                tables = {t for (t,) in ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                out = {"revision": (cfg.private / "revision").read_text().strip(),
                       "discovery": discovery_counts(ledger),
                       "outputs": dict(ledger.execute("SELECT status, COUNT(*) FROM output GROUP BY status").fetchall())
                       if "output" in tables else {}}
            finally:
                ledger.close()
    except RunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps(out, sort_keys=True), flush=True)
    if a.command == "run" and out.get("remaining"):
        return 3
    return 0
