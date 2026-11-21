# Attempt 2: diagnosis, changes, storage, runbook

Branch `claude/machina-attempt-2`. Attempt 1 (8a88c7d, state `ops/attempt-1`, corpus `attempts/attempt-1`)
is never written: attempt 2 opens it read only and only adds directory entries (hardlinks) elsewhere.

## Why 42k transforms kept approved identities (from residual accounting + format code)

`transformed_with_residual` = `output_detail.residual_approved > 0`. At 8a88c7d that count came from:

1. **STEP DATA, typed-kind gate** (`build_org_map`): an approved value found at `ORGANIZATION.name` was
   replaced only if approved under kind `org`. FILE_NAME *authors* are approved as `person`, and a value can
   hold only one kind, so an author repeated as `ORGANIZATION.name` was counted residual and left in place.
2. **STEP DATA PERSON never in scope**: `PERSON.id/last_name/first_name` (and split full names) untouched.
3. **ZIP STEP members got only the FILE_NAME stage**; `archive._scan` then raw-counted every DATA occurrence
   (`ORGANIZATION`/`PERSON`), which is the 483/492 ZIP hits in the 550-file server benchmark.

The counting is not inflated: each counted occurrence was a real approved value left in output bytes. No
reporting change was made; instead the values are now replaced and the scans re-run on the output.

## Changes (all exact-equality, approved values only)

- `step_data_transform.IDENTITY_SPEC`: ORGANIZATION `id`,`name`; PERSON `id`,`last_name`,`first_name`, plus a
  joined match (`first last`, `last, first`, `last first`) that writes the pseudonym split across the
  two fields. Same verifier as 5D: identical entity graph/arity/refs, identical numeric-token sequence,
  byte-identical skeleton, only planned literals changed. Geometry labels, PRODUCT, descriptions, units,
  numeric/short ids are never edited.
- `twopass.build_org_map`: approved under any kind -> single catalog pseudonym (consistent cross-format).
- ZIP (and nested ZIP) STEP members: header stage then DATA stage; independent raw rescan kept.
- Top-level STEP: new independent check `independent_residual.step_data_literal_substring` counts DATA
  literals still containing an approved value (e.g. free-text descriptions); such files are not
  `fully_clean`, so genuine leftovers stay visible.
- New DATA positions are **pass-2 lookups only** (`twopass.is_pass2_only`): never recorded or decided in
  pass 1, so pass-1 approvals are identical to attempt 1 -> the attempt-1 catalog is valid as-is.

Synthetic benchmark (`uv run python scripts/attempt2_benchmark.py DIR [ALT_SRC]`, 30 files: 24 STEP, 6 ZIP
with nested ZIP): baseline 8a88c7d 68 approved occurrences left, 12 residual files, 18/30 fully clean;
attempt 2 0 left, 0 residual, 30/30 fully clean (fields replaced 114 -> 278).

## Storage plan (E: free ~72.7 GB < 139.7 GB copy)

- `adopt --from-attempt attempt-1`: copies catalog, pseudonym store, seed (0600) and discovery rows into
  `ops/attempt-2`; requires the source frozen digest to match before and the copy to reproduce it. Refuses
  if attempt-2 has any state. **No new discovery or policy validation is required.**
- `run --reuse-from attempt-1`: per row the worker computes the attempt-2 result; if the attempt-1 output
  for the same manifest path is a regular file with identical size and streamed SHA-256 to what attempt 2
  would write, the parent `os.link`s it to a temp name in the attempt-2 directory, re-checks
  ino/dev/size/mtime against what was hashed, then `os.replace`s it in. No open-for-write, truncate,
  chmod or utime on any linked inode (link() itself bumps st_nlink/ctime, unavoidable). Otherwise the
  normal staged, fsynced, atomically renamed new file is written. Package step re-hashes every output
  against the attempt-2 ledger (exact manifest coverage).
- New bytes needed ≈ only changed outputs (STEP/ZIP transforms that now differ) + their staging; reserve
  check (`--reserve-bytes`) still guards each submission.
- Fallback when linking is impossible (different filesystem): the runner refuses `--reuse-from`; use
  separately approved output storage via `--root` on that volume with the same layout (no provisioning
  here).

## Commands (WSL; `ROOT` = the existing workspace root)

```bash
ROOT=/mnt/machina-deid                     # the live layout; attempt 1 is untouched
REPO2=$ROOT/repo-attempt-2                 # NEW checkout; never $ROOT/repo (live job)
git clone --branch claude/machina-attempt-2 <origin> $REPO2 && cd $REPO2
NEW=$(git rev-parse HEAD)
uv sync --frozen --all-extras              # private .venv inside REPO2; live env untouched
PY=$REPO2/.venv/bin/python
uv run pytest -q                           # must be green

# pilot: synthetic benchmark (no private data)
$PY scripts/attempt2_benchmark.py /tmp/a2-bench

# one-time state adoption (attempt-1 runner must not be running)
$PY scripts/run_full_corpus.py adopt --root $ROOT --repo $REPO2 --attempt attempt-2 --from-attempt attempt-1

# representative local pilot: stop after N outputs, inspect redacted status, resume later
$PY scripts/run_full_corpus.py run --root $ROOT --repo $REPO2 --attempt attempt-2 --reuse-from attempt-1 \
    --expected-revision $NEW --workers 10 --stop-after-outputs 2000 --no-package
$PY scripts/run_full_corpus.py status --root $ROOT --attempt attempt-2

# full run (resumable; rerun the same command after any interruption)
nohup $PY scripts/run_full_corpus.py run --root $ROOT --repo $REPO2 --attempt attempt-2 \
    --reuse-from attempt-1 --expected-revision $NEW --workers 10 > $ROOT/ops/attempt-2.log 2>&1 &
```

Redacted aggregate checks to send back: `status` JSON, packaged summary (`transformed_with_residual`,
`transformed_fully_clean`, outcomes), and `df -h` for E:.

## Runtime / memory assumptions

Linked rows cost one streamed read of input + one of the attempt-1 output (no write). Transform rows are
unchanged from attempt 1 plus one DATA reparse per STEP (≤64 MiB, ~3-5x file size in RAM per worker for
the in-memory latin-1 text and record table). Expect total runtime ≈ attempt 1 transform phase + ~15%;
no discovery phase. Ten workers with 2 heavy slots fit in the same memory envelope as attempt 1.
