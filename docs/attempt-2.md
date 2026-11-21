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

## Attempt 2b: the one-hit STEP effect, new formats, pilot

**Why exactly one hit per file (41,956/41,956).** Every residual-positive transformed STEP row had
`residual_approved == 1`. In 8a88c7d/c960298 a *transformed* STEP row can only get residual from
(a) `mapped_present_in_unrequested_fields` (an approved FILE_NAME value repeated in a header field the
transform does not edit: `preprocessor_version` or `time_stamp`), (b) DATA `unmodifiable` (counted for
*every* undecodable ORGANIZATION/PERSON literal, approved or not), or (c) the attempt-1 typed-org gate (one
per distinct value; fixed in c960298). Header `unmodifiable` makes the row *failed*, not residual. Exporters
that write the organization/user into `preprocessor_version` produce exactly one hit per file, file after
file; `tests/test_twopass.py` even pinned that behaviour. The synthetic benchmark reproduces it on
c960298 (8/8 preprocessor-variant files, 1 hit each). The d13f849 pilot's surviving residual fits (a)/(b),
not (c). This is an inference from code + counts; the pilot below measures it on real rows.

**Fixes (approved values only; no catalog change; pass-2 only):**
- `step_transform.PASS2_FIELDS` = default + `preprocessor` (header verifier unchanged). `time_stamp` is never
  edited and still counted.
- DATA `unmodifiable` counts an undecodable literal only if its raw utf-8/cp1252 form or best-effort
  directive decoding contains a mapped value.
- `step_data_transform.transform_descriptive`: boundary-aware substring replacement of *this file's own*
  approved values in `PRODUCT` id/name/description, `PRODUCT_DEFINITION(_FORMATION[_WITH_SPECIFIED_SOURCE])`
  id/description, `NEXT_ASSEMBLY_USAGE_OCCURRENCE` name/description, `ORGANIZATION.description`. Same
  verifier (graph/arity/refs, numeric tokens, skeleton, unplanned literals) plus a post-check. Literals with
  directives/8-bit bytes are left and counted. Geometry/representation labels are never edited and remain
  in `independent_residual.step_data_literal_substring`. Also applied to ZIP STEP members.
- **SVG** (`svg_meta`): whole-value text of `dc:creator|publisher|contributor|rights` (direct or
  `cc:Agent/dc:title`) equal to a catalog value approved as `person`/`org`. Splice only; parser-vs-span
  agreement, full element/attribute/tail comparison. DTD entities, non-UTF-8 and non-`svg` roots are refused.
  `<title>`/`<desc>` free text is out of scope and is reported as `svg_raw_substring`.
- **STL** (`stl_meta`): binary 80-byte header text, ASCII `solid`/`endsolid` names. Boundary-delimited
  spans (1-4 tokens) equal to `person`/`org` approvals. Binary: body bytes `[80:]` and size identical.
  ASCII: every non-solid line byte-identical, numeric tokens identical.
- SVG/STL candidates are pass-2 only (`is_pass2_only`), so a rerun of discovery records nothing new.

**Not implemented, on purpose:** SAT. ACIS headers hold product/version/date only, with no user or
organization field. SolidWorks/Inventor OLE property sets: preserving every program-critical stream can't be
proven without the vendor application, so these are unchanged passthrough. PDF lexical-boundary visible
text: unchanged. FCStd `Document.xml` `CreatedBy`/`LastModifiedBy`/`Company` (4,717 files) is the
most promising next format and is not done yet.

**Compatibility:** outputs change only where a new stage fires. Every other output stays byte-identical
to attempt 1 and is still hardlinked by `--reuse-from`. Incompatible cases are marked, not hidden: an
attempt-1 SVG/STL row that was `unsupported_passthrough` can now become `transformed`,
`no_approved_identities` or `failed` (status changes; bytes change only for `transformed`).

**Synthetic benchmark** (`scripts/attempt2b_benchmark.py`, 24 STEP + 6 STL + 6 SVG, no real data):

| | c960298 | attempt 2b |
|---|---|---|
| STEP approved occurrences left / before | 24 / 96 | 0 / 96 |
| STEP reported-residual files (1 hit each) | 8 | 0 |
| STEP fully clean | 8/24 | 24/24 |
| STEP fields replaced | 72 | 96 |
| STL+SVG transformed / left | 0 / 12 | 12 / 0 |
| source preserved | 36/36 | 36/36 |

`scripts/attempt2_benchmark.py` (the original 30-file set) is unchanged at 278 fields, 30/30 clean.

### Pilot / diagnostic (read only, counts only)

`scripts/attempt2_residual_pilot.py` opens the attempt-1 ledger with `mode=ro`, checks that the adopted
catalog equals the frozen digest, and picks samples ordered by `sha256(path)`:
residual-positive transformed rows per extension, a residual-free control set, and attempt-1
SVG/STL passthrough rows. For each sample it checks the attempt-1 output against the ledger digest,
hashes the input before and after, re-transforms into `--scratch` (refused inside
`input/`, `attempts/` or `ops/`, deleted afterwards), and reports old vs new residual, status/reason codes,
change counters, hardlinkability, and STEP input attribution by location type (`header:<field>`,
`data:identity_*`, `data:descriptive_scoped_*`, `data:other_entity:<SCHEMA_TYPE>`). It never prints
paths, values or pseudonyms.

```bash
cd $REPO2 && git pull --ff-only && NEW=$(git rev-parse HEAD) && uv run pytest -q
$PY scripts/attempt2b_benchmark.py /tmp/a2b-bench-$$            # synthetic, no private data
$PY scripts/attempt2_residual_pilot.py --root $ROOT --attempt attempt-2 --ref attempt-1 \
    --scratch /tmp/a2b-pilot-$$ --per-ext 200 > /tmp/a2b-pilot.json   # send this JSON back
# 2,000-output local pilot. If ops/attempt-2 is already pinned to c960298 the runner refuses the new
# revision (by design, no mixed outputs): adopt a fresh attempt name from attempt 1 instead.
$PY scripts/run_full_corpus.py adopt --root $ROOT --repo $REPO2 --attempt attempt-2b --from-attempt attempt-1
$PY scripts/run_full_corpus.py run --root $ROOT --repo $REPO2 --attempt attempt-2b --reuse-from attempt-1 \
    --expected-revision $NEW --workers 10 --stop-after-outputs 2000 --no-package
$PY scripts/run_full_corpus.py status --root $ROOT --attempt attempt-2b
```

## Runtime / memory assumptions

Linked rows cost one streamed read of input + one of the attempt-1 output (no write). Transform rows are
unchanged from attempt 1 plus one DATA reparse per STEP (≤64 MiB, ~3-5x file size in RAM per worker for
the in-memory latin-1 text and record table). Expect total runtime ≈ attempt 1 transform phase + ~15%;
no discovery phase. Ten workers with 2 heavy slots fit in the same memory envelope as attempt 1.
