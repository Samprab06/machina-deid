# Attempt 2c: PDF scoped de-identification, private annotation and evaluation

Score 292 diagnosis: PDF scoped removal 0/300, same identity 0/200, triplets 0/100. Only 63/10,342 PDFs were
transformed; 10,187 had *no approved candidates*. Cause (code): PDF discovery read only `LABEL: value` from the
plain text stream (`discovery._label_values`). Drawing title blocks put the role label and its value in separate
cells/lines, so nothing was discovered, nothing approved, nothing rewritten. Values approved in other formats
(STEP/DXF authors, organizations) were never looked up in PDFs either, so cross-document consistency could not
score.

## Modules

| module | role |
| --- | --- |
| `pdf_titleblock.py` | char-level lines from `rawdict` (multi-run, any direction); split table cells; role labels (drawn/checked/approved/designed/engineer/qa/company/customer/project/…); value right of or below the label **in the label's own text frame** (rotated title blocks); inline `DRAWN BY: x`; email / URL-domain / phone patterns (phone needs `+`/`(` or a phone label); n-gram lookup of unlabelled text |
| `pdf_entities.py` | pass 1 `EntityMapBuilder` → **frozen** SQLite entity map (HMAC value keys only, never overwritten, digest + secret fingerprint); kind voting; ambiguous values (conflicting kinds) are never replaced; conservative corroborated aliases (org legal-suffix variants with a unique stem; `J. Doe` → the *unique* full name with that initial+surname *in the same family*); catalog-approved values keep the catalog pseudonym; fitted short forms `PE-<token>`; `Resolver` for pass 2 |
| `pdf_scoped.py` | plan → text-only redaction of each occurrence's char extent (images/vector graphics untouched) → pseudonym at original origin/direction/colour, shrunk to fit, fitted form when < 55 % size → metadata/XMP/annotations/forms/bookmarks/attachment descriptors substitution → **verification** (see below). Any failure discards the output |
| `pdf_eval.py` | independent evaluator (own extraction code; does not import the pipeline) |
| `pdf_lab.py` | private local CLI: `sample`, `survey`, `propose`, `build-map`, `run`, `evaluate` |

Per-edit verification on the re-opened output: original absent from the edit region; replacement extractable
there; replacement renders (pixmap ink); every char outside all edit regions identical (glyph + position);
vector drawings, images, links, page boxes/rotation unchanged; independent whole-document residual
(`pdf_residual.inspect_pdf`) of every resolved value is zero.

## Labels (private; never commit)

`labels.jsonl`, one occurrence per line, PyMuPDF text-space coordinates (unrotated page):

```json
{"doc": "rel/path.pdf", "page": 0, "bbox": [x0, y0, x1, y1], "text": "…", "kind": "person", "entity": "E17", "status": "confirmed"}
{"doc": "rel/path.pdf", "page": 0, "bbox": [x0, y0, x1, y1], "text": "SCALE 1:2", "type": "keep", "status": "confirmed"}
```

`entity` ids are the annotator's own identity clusters (same person in different documents = same id).
`status: proposed|rejected` rows are ignored by `evaluate`. `family` (optional) overrides the default
(parent directory) used for the holdout.

## Metrics and failure classes

`scoped_removal` (occurrence: original absent in its region, a different replacement extractable and rendered;
fallback/unchanged copy or missing output always fails), `same_identity` (pairs within an entity, both
successful and same pseudonym token — full and fitted forms share the token), `cross_document_triplets`
(triples of an entity's occurrences in 3 different documents; our proxy for the triplet metric),
`distinctness_collisions`, `engineering` (geometry, drawings, images, keep-labels, chars changed outside label
regions — unlabelled replacements show up there as a count, not a failure).

Failure classes with `--trace`: `discovery` (no pipeline record overlapping the label), `resolution`
(found but ambiguous / not in frozen map), `rewrite` (edit failed its verification), `validation` (pipeline
claimed `verified`, evaluator disagrees — a pipeline bug), `fallback_copy`, `missing_output`.

Holdout (`--holdout-frac F --seed-file S`): split by family; families sharing any byte-identical input are
joined first so duplicates never leak across dev/holdout; reported as `all`, `dev`, `holdout`.

## Local commands (all private paths outside the repo; stdout = redacted aggregates to relay)

```bash
cd $REPO && uv sync --frozen --all-extras
L=/path/private/pdf-lab; IN=/path/to/raw/corpus           # never inside the repo
uv run python -m machina_deid.pdf_lab sample --input $IN --n 120 --seed-file $L/seed --out $L/list.txt
uv run python -m machina_deid.pdf_lab survey --input $IN --list $L/list.txt            # relay this JSON
uv run python -m machina_deid.pdf_lab propose --input $IN --list $L/list.txt --out $L/draft.jsonl --crops $L/crops
#   human: copy draft.jsonl -> labels.jsonl; set status=confirmed/rejected; fix boxes; add missed occurrences
#   (role_label_hint rows show where to look); assign entity ids; add a few type=keep rows
uv run python -m machina_deid.pdf_lab build-map --input $IN --list $L/all-pdfs.txt --secret-file $L/secret \
    --map $L/entity-map.sqlite [--catalog $ROOT/ops/attempt-2/catalog.sqlite]
uv run python -m machina_deid.pdf_lab run --input $IN --list $L/list.txt --output $L/out \
    --secret-file $L/secret --map $L/entity-map.sqlite                                  # relay this JSON
uv run python -m machina_deid.pdf_lab evaluate --input $IN --output $L/out --labels $L/labels.jsonl \
    --trace $L/out/trace.jsonl --holdout-frac 0.3 --seed-file $L/seed                   # relay this JSON
```

`--derive` (instead of `--map`) skips pass 1 for a quick look; cross-document lookups then only cover values
seen earlier in the same document. Baseline check: `evaluate --output $IN` (unchanged input) must score 0 with
only `fallback_copy` failures.

Measurements we need back: `survey`, `run`, `evaluate` JSON (all splits); for misses, the failure-class counts
by kind are enough to direct the next change.

## OCR / textless pages

`survey` reports first-page classes `text`, `image_scan` (> 50 % image cover), `vector_outlined` (no text,
> 50 vector paths: text drawn as outlines), `empty_or_sparse`, plus `ocr.tesseract`. No OCR engine is installed
here; `transform_pdf_scoped` counts `textless_pages` and never claims them clean. Outlined/scanned title blocks
are a known gap: redaction would need OCR boxes plus raster/vector edits, which this slice does not do.
