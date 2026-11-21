# Boilerbuild Attempt 2 handoff

Start with the production branch `claude/machina-attempt-2`. The last tested pipeline revision was `3de1cd5`; it matches the source tested at `65ce09f` from PR #1. This page takes precedence over older Attempt 2 notes about PDF and OLE support.

## Current evidence

- Attempt 1 scored 292/1,000. All three PDF categories and the two OLE account categories scored zero. Preserve drawing/model correspondence, engineering data, OLE previews, and container integrity.
- The 67-file materialized smoke package passed every output checksum. It rewrote 12 of 37 PDFs, 18 of 18 sampled SolidWorks OLE files, six STEP files, and six ZIPs. Eight PDFs failed validation and 17 had no approved identity. No approved identity is a processing outcome, not a verified absence of identifying content.
- An independent reader checked 210 sampled SolidWorks compound files. All 210 had new saved accounts and matching stream trees. All 200 available named previews were unchanged. Four of 195 files with accounts at least four characters long retained raw UTF-16 substrings of the old account. Those matches extended into longer tokens. Account equivalence for those tokens remains unverified.
- A loose whole-document person lookup proposed thousands of unscoped replacements. At the same map and 111-file sample, the constrained candidate proposed 32 unlabelled person matches. It transformed 32 of 111 PDFs. Keep the contextual checks when improving recall.
- The path audit found 16,773 exact identity path candidates and 178,380 files in unaudited reference formats. Treat the audit as discovery. Rename only once incoming references can be resolved, rewritten, and verified together.
- The 111-PDF manifest contains 57 development and 54 holdout items across 37 directory groups. No source SHA-256 appears in both sides of the frozen file sample. Directory grouping is a proxy for a drawing family. Labels and pair/triplet denominators still need expansion.

## Local environment and private artifacts

Run corpus work in WSL distro `Machina`, rooted at `/mnt/machina-deid` on E:. The active checkout is `ops/attempt-2-candidate/repo`, Python is `ops/venv/bin/python`, and the official input is `input/`. Tesseract has English, French, and German data. Use `OMP_THREAD_LIMIT=1` for parallel OCR.

`ops/score-evaluation/` contains the private frozen sample (`sample-v1.json`, `all.list`, `dev.list`), unreviewed proposals (`pdf-review-draft-v1.jsonl`), a small independent literal-email reference, the 72-file independent OLE fixture list, and the production pilots. The proposal file has 177 candidates and 146 role-label hints. Treat proposals as prompts for review, and add occurrences that discovery missed. Label values, bounding boxes, directories, seeds, original corpus, and upload credentials stay local. Only relay aggregate counts and failure codes to remote Claude Code. The server checkout is for source and synthetic fixtures.

## Continue the PDF benchmark

Independently review the selected pages and write a private `labels-confirmed.jsonl`. Record every occurrence in reviewed regions, include geometry or dimension controls as `type: keep`, and use the same entity ID across documents only when the evidence supports it. Work on the development families first. Keep the frozen holdout for milestone comparisons. Inspect differently named and similarly named people, title blocks, Info/XMP, form/annotation content, and textless pages.

```bash
ROOT=/mnt/machina-deid
cd "$ROOT"
REPO="$ROOT/ops/attempt-2-candidate/repo"
LAB="$ROOT/ops/score-evaluation"
PY="$ROOT/ops/venv/bin/python"
export PYTHONPATH="$REPO/src"
"$PY" -m machina_deid.pdf_lab propose --input input --list "$LAB/all.list" \
  --out "$LAB/pdf-review-next.jsonl" --max-pages 3
"$PY" -m machina_deid.pdf_lab evaluate --input input --output "$LAB/pdf-corpus-f9b7431-out" \
  --labels "$LAB/labels-confirmed.jsonl" --trace "$LAB/pdf-corpus-f9b7431-out/trace.jsonl"
```

The output checkout above is historical smoke output. Repeat with outputs materialized from the current candidate and report denominators. Use `sample-v1.json` to preserve the existing split, for example by partitioning confirmed labels into two JSONL files without changing family membership. The evaluator's optional `--holdout-frac` creates a different split and joins duplicate families. `same_identity` scores cross-document repeats. `distinct_identity_triplets` scores three different entities. The older `cross_document_triplets` counts same-identity repeats across three documents and is a separate diagnostic. All metrics are local proxies for the hidden scoring checks.

## Stratified pilot on `3de1cd5`

The 512-file local pilot is `ops/score-evaluation/stratified-v1`. It includes randomly drawn files within PDF, SolidWorks, Inventor, STEP (previously changed and unchanged), ZIP, other CAD, and other strata. It excluded files over 100 MB and intentionally oversampled candidate formats. Input and reference files are hardlinked to the originals. The runner used six workers, OCR, and the physical disk guard.

- Wall time was 3 minutes 20 seconds. All 512 outputs passed size and SHA-256 checks. The runner transformed 151 files and reused 358 files by inode. New output blocks totaled about 121 MB. Windows E: free space moved from 71.730 GB to 71.696 GB during the run. Physical VHDX growth and new ext4 file blocks measure different things. Neither gives a whole-corpus estimate from this sample alone.
- Of 160 PDFs, 59 transformed, 71 had no approved identity, 25 failed validation, and five were unsupported. The 25 validation failures comprised 16 residual, four out-of-region text, three link, and two vector geometry failures.
- The independently extracted Poppler email set has 17 occurrences in five documents, with no cross-document repeat denominator. Attempt 1 cleared zero and the pilot cleared three. Fourteen misses were byte-identical fallbacks in two link-validation failures. One HTTP URI was converted into a launch/file action during substitution, losing its scheme. The other file retained five identifying mailto actions despite visible edits. The latter also had one remaining residual outside the link check.
- Next diagnostic for those two files: compare normalized source/output actions and raw PDF action dictionaries locally. Preserve the URI scheme and meaningful components when substituting a sanitized value. Check URI strings and indirect action dictionaries, including percent encoding. Keep link validation enabled. A quoting and boundary-only experiment did not resolve either real file and was discarded.

```bash
cd /mnt/machina-deid
PILOT=ops/score-evaluation/stratified-v1
REPO=ops/attempt-2-candidate/repo
export PYTHONPATH="$PWD/$REPO/src"
ops/venv/bin/python -m machina_deid.pdf_lab evaluate --input "$PILOT/input" \
  --output "$PILOT/attempts/candidate/corpus" --labels "$PILOT/poppler-reference-v1.jsonl"
```

## Run, package, and submit

1. Fix discovery and resolution first. Then triage failures by residual surface, text outside the edit region, changed vector geometry, and link differences. Keep a positive independent check for every supported format. Regression coverage starts with `tests/test_pdf_eval.py`, `tests/test_pdf_scoped.py`, `tests/test_pdf_ocr.py`, `tests/test_scope.py`, and `tests/test_ole_transform.py`.
2. Before a full run, review the actual change rate and wall time by format, including OCR and large ZIPs. E: free space is `df -B1 /mnt/e` inside WSL. The ext4 virtual disk's reported free space is a different measurement. Keep a 20 GB physical reserve and prefer byte-identical hardlink reuse. The sample excludes files above 100 MB, so use it for a preliminary estimate.
3. Once the candidate and timing are acceptable, use a fresh attempt name and pin the full commit. Adopt the completed Attempt 1 discovery snapshot if it validates, then build the new shared scope map as part of the run. Resume using the exact same revision, options, and attempt name after a safe stop. An existing pilot pinned to a different revision needs a new attempt directory.

```bash
REV=$(git -C "$REPO" rev-parse HEAD)
export OMP_THREAD_LIMIT=1
"$PY" "$REPO/scripts/run_full_corpus.py" adopt --root "$ROOT" --repo "$REPO" \
  --attempt attempt-2 --from-attempt attempt-1
"$PY" "$REPO/scripts/run_full_corpus.py" run --root "$ROOT" --repo "$REPO" \
  --attempt attempt-2 --expected-revision "$REV" --scope-ocr --workers 6 \
  --reuse-from attempt-1 --host-space-path /mnt/e --host-min-free-bytes 20000000000
```

4. Check the Builders deadline in the signed-in team panel and reserve 60–90 minutes for upload, remote inventory verification, and queueing. The previous upload took about 32 minutes. Check manifest counts and digests, the package's provenance, and real-file PDF/OLE preservation before uploading. For a credible 900-point attempt, aim for at least 95% on each of the three PDF proxy metrics and at least 90% on both OLE account measures, with strong measured preservation. If time forces a lower-confidence attempt, present the measured tradeoff to Samridh.
5. Obtain Samridh's approval before queueing Attempt 2. The Builders write token lasts one hour. Request it only when the package is ready and keep it out of Git and chat. If approval becomes urgent, contact Samridh through the configured Hermes Telegram target. Verify that Builders shows the attempt in scoring after submission.
