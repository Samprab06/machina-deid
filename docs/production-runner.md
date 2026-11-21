# Production runner (parallel, resumable)

`scripts/run_full_corpus.py` → `machina_deid.production`. It replaces the serial local reference runner
but keeps all of its on-disk state:

```
<root>/input/.get-input/manifest.tsv     official manifest (read only)
<root>/input/<path>                      raw corpus, never written
<root>/repo                              clean checkout of the pinned revision
<root>/ops/download.exit                 downloader exit status
<root>/ops/attempt-1/{ledger.sqlite,catalog.sqlite,catalog.pseudonyms.json,seed,revision}
<root>/attempts/attempt-1/{corpus,code,README.txt}
```

`--root` defaults to `/mnt/machina-deid`; point it at any synthetic workspace with the same layout.

## What it does

1. **Discovery (ordered, parallel parse).** Each sweep picks the available input files whose size
   matches the manifest (the same `verified_input` rule as before). It sends them to worker processes
   in bounded batches (`--batch-files`, `--batch-bytes`, at most `--heavy-slots` heavy files per batch).
   Workers parse only and never open the catalog. The parent records results in manifest order and
   writes one `discovery` row per file (`inspected | unsupported_format | discovery_exception`), so
   first-decision-wins approvals match a serial run over the same availability. Sweeps repeat every
   `--poll-seconds` until every row is discovered, so the catalog keeps up with the download. A
   non-empty `ops/download.exit` with rows still missing after one more sweep stops the run with a
   count.
2. **Freeze.** After all discovery is done, the catalog WAL is checkpointed, the journal is switched to
   rollback mode, and a digest of `catalog.sqlite` plus the pseudonym store is saved in
   `runner_meta`. The digest is checked again before and after the transform and on every resume. A
   changed catalog aborts the run.
3. **Transform (parallel).** Each worker opens the frozen catalog read only, writes one staging file
   under `ops/attempt-1/staging/` (same filesystem as the corpus) and fsyncs it. Only the parent
   renames staging files into `corpus/<exact manifest path>`, fsyncs the directory and commits
   `output` rows in groups of 32. Statuses are unchanged: `transformed | no_approved_identities |
   failed | unsupported_passthrough`. Every status other than `transformed` is a byte-identical copy,
   checked with streamed SHA-256 before it is accepted. The source is hashed before and after
   (raw immutability). With manifest MD5s present, the input MD5 is checked in the same read
   (`--no-verify-md5` turns this off). The reason, residual-approved count, fields replaced and
   fully-clean flag go to the additive `output_detail` table.
4. **Crash isolation.** If a worker dies (for example from OOM), the files it had in flight are retried
   one at a time in a single-worker pool. A file that kills its worker again is copied byte-for-byte
   by the parent with status `failed`, reason `worker_crash`.
5. **Package.** Checks that coverage matches the manifest and the ledger, re-hashes every output in
   parallel streamed reads, counts organizer limits (more than 2,000,000,000 bytes per file, more than
   400,000 objects → `upload_blocked_by_limits`), and writes `code/` (git archive, runner script,
   `run-dependencies.txt`) and `README.txt`.

Resume always works: kill the runner at any point and run the same command again. Staging is wiped on
start. An output renamed but not yet committed is regenerated.

## Readiness and privacy gates

- `run` requires `--expected-revision <40-hex>`. It must equal `git -C repo rev-parse HEAD` (clean
  checkout) and the pinned `ops/attempt-1/revision`.
- Manifest coverage must be exactly `--expected-rows` (default 188,722), unique, with no absolute or
  `..` paths.
- Transformation never starts before discovery is complete. The catalog is immutable from then on.
- An exclusive `flock` on `ops/attempt-1/runner.lock` stops two runners from using one attempt.
- The free-space reserve (`--reserve-bytes`, default 15 GiB) is checked before every batch and task.
- Logs, summaries and `status` show counts and status codes only. They never show paths, values or
  exception text. The seed, ledgers, catalog and logs stay in `ops/` and are never copied into
  `attempts/`.
- **Pass-through files (`unsupported_passthrough`, `no_approved_identities`, `failed`) are not
  de-identified**, and the README says so. `transformed_with_residual` counts transformed files that
  still hold approved values. `failed_reasons` reports why transforms failed.

## Limits

- Discovery speedup is limited: the parent records in order (a single SQLite writer) and caps it.
  Parsing is the parallel part. The transform phase scales with workers.
- ZIPs and files of `--heavy-bytes` (256 MiB) or more are "heavy". At most `--heavy-slots` (2) run at
  the same time. Archive members are held in memory up to `ArchiveLimits` (256 MiB per member, 1 GiB
  total, depth 2, 10k members). Over-limit archives are reported as failed and passed through.
- There is no per-file timeout. A hung parser stalls one worker slot until it is killed (resume is
  safe).
- DXF outputs are not bit-reproducible across runs: ezdxf stamps `$TDUPDATE`/`$TDUUPDATE`, a fresh
  `$VERSIONGUID` and a write-time marker on every save, serial runs included. All other formats,
  every status and every non-DXF digest are reproducible across worker counts and resumes.
- Discovery of large manifests restats every pending file on each sweep. That costs seconds for
  188k rows.

## Sizing for local WSL (16 logical CPUs, 15 GiB RAM)

`--workers 12 --heavy-slots 2 --batch-files 256` leaves CPU for the downloader and WSL. Expected memory:
12 × ~0.3 GiB for typical PDF/DXF/STEP, plus 2 heavy slots × ≤ ~2.5 GiB (1 GiB archive budget plus
parse overhead), about 9 GiB. If WSL comes under memory pressure, lower `--workers` to 8 or
`--heavy-slots` to 1. `--workers 1` is the single-process fallback (same code path, serial).

## Measured synthetic throughput

`python scripts/production_throughput.py --groups 40 --workers 1 2` (241 mixed synthetic files; this
dev box has 2 aarch64 CPUs):

| workers | discovery s | transform s | files/s | speedup |
|---|---|---|---|---|
| 1 | 2.22 | 3.22 | 44.3 | 1.00 |
| 2 | 2.43 | 1.86 | 56.1 | 1.27 (transform 1.73×) |

Outputs were identical with DXF save metadata masked. Run the same script on WSL with `--workers 1 12`
before adoption to measure local scaling.

## Dependencies

Python ≥ 3.11 (3.12 on WSL), with `pip install -e '.[pdf,cad,test]'` (pypdf, pymupdf, ezdxf, pytest).
Stdlib only otherwise: sqlite3, multiprocessing forkserver, fcntl, so Linux/WSL only. `git` and `tar`
are needed for packaging.

## Adoption from the live dc3da69 run (orchestrator)

Run only while the live job is in discovery with no committed outputs; `migrate` enforces this.

```sh
ROOT=/mnt/machina-deid
NEW=<pushed commit of claude/machina-submission-readiness>
PY=<the python of the env the live job uses>

# 1. Stop the live runner only (leave the downloader alone). Discovery commits per file, so this is safe.
pgrep -af 'run_full_corpus|local-runner'      # identify the live runner PID
kill -INT <PID>; sleep 5; pgrep -af 'run_full_corpus|local-runner'   # must be gone

# 2. Move the checkout to the new revision.
git -C $ROOT/repo fetch origin claude/machina-submission-readiness
git -C $ROOT/repo checkout --detach $NEW
test -z "$(git -C $ROOT/repo status --porcelain)"
$PY -m pip install -e "$ROOT/repo[pdf,cad]"    # dependency set is unchanged

# 3. Explicit migration: checks for no outputs, empty corpus, ledger integrity, and that the catalog
#    opens with the existing seed; backs up ledger/catalog/store to ops/attempt-1/pre-migration-dc3da69/,
#    writes ops/attempt-1/migration.json, re-pins revision -> $NEW.
$PY $ROOT/repo/scripts/run_full_corpus.py migrate --root $ROOT \
    --from-revision dc3da69225167cfe81bfbd5803ed02902efd0219 --expected-revision $NEW

# 4. Start the runner (continues discovery while downloading, then transforms and packages).
nohup $PY $ROOT/repo/scripts/run_full_corpus.py run --root $ROOT --expected-revision $NEW \
    --workers 12 --heavy-slots 2 > $ROOT/ops/attempt-1/runner.log 2>&1 &
$PY $ROOT/repo/scripts/run_full_corpus.py status --root $ROOT    # redacted counts
```

Exit codes: 0 means done (packaged), 2 means a gate or error (message on stderr), 3 means rows still
need a rerun (for example an input MD5 mismatch; see the logged `errors`).

## Rollback

- **Before any output** (`status` shows no outputs): stop the runner, then run
  `$PY $ROOT/repo/scripts/run_full_corpus.py rollback-migration --root $ROOT`. This re-pins dc3da69
  and clears the freeze marker. Then `git -C $ROOT/repo checkout --detach dc3da69` and restart the old
  runner. The discovery rows and catalog written meanwhile use identical dc3da69 discovery semantics,
  and the old runner reuses them.
- **After outputs exist:** prefer to stop and resume the new runner (`--workers 1` if parallelism is
  the suspect). To return to the old serial runner, stop, check out dc3da69 and write its full SHA to
  `ops/attempt-1/revision`. It skips outputs already in `output`, which have identical semantics. It
  opens the catalog read-write, which changes the frozen digest, so do not switch back to the new
  runner afterwards without a fresh review.
- A full restore of the pre-migration state is in `ops/attempt-1/pre-migration-dc3da69/` (SQLite
  backups plus the pseudonym store). The seed is never copied.

## Group-commit discovery checkpoints (upgrade from 27757e3)

**Why:** in 27757e3 each manifest row paid for 1 catalog COMMIT for its observations, 1 COMMIT per
distinct auto-decided value, a full rewrite and fsync of the pseudonym JSON for each new approved identity
(cost grows roughly with the square of the identity count), and 1 ledger COMMIT (rollback journal,
`synchronous=FULL`). On an ext4 loop file over WSL DrvFS this kept the parent in D state at about 4–6 rows/s.

**Now:** each discovery batch (`--batch-files`, default 256, also capped by `--batch-bytes`) is checkpointed as:

1. all catalog work for the batch runs in one `BEGIN IMMEDIATE`. Each catalog operation is a SAVEPOINT,
   so a failing operation rolls back exactly as before, and reads see the batch's own writes, so
   auto-approval results are identical;
2. the pseudonym store is written once (atomic temp file, fsync, rename, directory fsync);
3. the catalog COMMITs (WAL, `synchronous=FULL`, unchanged);
4. the batch's ledger rows are inserted in manifest order and COMMITted once (`synchronous=FULL`, unchanged).

No PRAGMA was weakened. **Crash invariant:** a ledger row is never durable unless its catalog effects
and pseudonyms are. A crash before step 3 loses only that batch's uncommitted work. A crash between
steps 3 and 4 leaves catalog rows without ledger rows, so those rows are re-discovered on resume. All
catalog writes are idempotent (`INSERT OR IGNORE` observation keys; auto decisions never override; deterministic
HMAC pseudonyms), so resume never doubles observations or changes pseudonyms. A store with extra entries is
valid; the reopen check only requires catalog ⊆ store. Tests:
`tests/test_group_commit.py` (state equivalence with per-row commits for batch 1/7/64 and workers 1/3; a
real process crash between the catalog and ledger commits followed by resume; group rollback; SAVEPOINT isolation;
one store write per group; 27757e3 → new migration and rollback).

**Measured** (`scripts/discovery_commit_benchmark.py`, checkpoint I/O only, synthetic, local ext4 on
aarch64; state hash identical in every case):

| rows | distinct identities | per-row rows/s | group rows/s | speedup |
|---:|---:|---:|---:|---:|
| 1000 | 5 | 155 | 4365 | 28× |
| 2000 | 500 | 102 | 3035 | 30× |
| 3000 | 3000 | 29 | 671 | 23× |

Run it on the real work directory's filesystem to check before rollout (no real data is read):
`$PY $ROOT/repo/scripts/discovery_commit_benchmark.py --rows 2000 --identities 500 --dir $ROOT/ops`.

**Limits:** one pseudonym-store rewrite per batch still grows with store size (O(store) per batch, not
per identity). Parsing is now the expected discovery bound. Up to one batch of discovery work is redone
after a crash. The transform phase still fsyncs each staged output and its directory (this is required
for publish durability); if transforms become I/O-bound, that is the next candidate (batched directory fsyncs).

### Rollout (live run pinned to 27757e3, discovery phase, no outputs)

```sh
ROOT=/mnt/machina-deid; PY=<python of the live env>
NEW=<pushed commit of claude/machina-submission-readiness>
OLD=27757e305f9dbc8e4b607f991a6491734a7091ed
pgrep -af 'run_full_corpus|local-runner'; kill -INT <RUNNER_PID>     # NOT the downloader
sleep 5; pgrep -af 'run_full_corpus|local-runner'                    # must be empty
git -C $ROOT/repo fetch origin claude/machina-submission-readiness
git -C $ROOT/repo checkout --detach $NEW && test -z "$(git -C $ROOT/repo status --porcelain)"
$PY $ROOT/repo/scripts/run_full_corpus.py migrate --root $ROOT --from-revision $OLD --expected-revision $NEW
#   -> backs up to ops/attempt-1/pre-migration-27757e3/, writes migration.json, re-pins $NEW
nohup $PY $ROOT/repo/scripts/run_full_corpus.py run --root $ROOT --expected-revision $NEW \
    --workers 8 --heavy-slots 2 >> $ROOT/ops/attempt-1/runner.log 2>&1 &
grep 'Discovery checkpoint' $ROOT/ops/attempt-1/runner.log | tail -3   # should advance per batch
```

The dc3da69 migration path is unchanged (`--from-revision dc3da69225167cfe81bfbd5803ed02902efd0219`).

### Rollback (before any output)

Stop the runner, then run `$PY $ROOT/repo/scripts/run_full_corpus.py rollback-migration --root $ROOT`.
This re-pins 27757e3. Then run `git -C $ROOT/repo checkout --detach $OLD` and restart the old runner
the same way. The on-disk formats are identical (same tables, pragmas and store format), and rows
discovered by the new runner are reused as is. A full pre-upgrade copy is in
`ops/attempt-1/pre-migration-27757e3/`.
