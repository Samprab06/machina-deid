# Identity catalog (Phase 5A)

`src/machina_deid/catalog.py` provides `IdentityCatalog`, a persistent SQLite catalog of identities and
where they were observed. It complements `identity.py` (pseudonyms) and `discovery.py` (candidates). No
transformer reads it yet.

## Storage and privacy

- SQLite (WAL, `synchronous=FULL`, file mode 0600). The schema is versioned through `PRAGMA user_version`
  migrations. Opening a catalog with a newer schema than the software supports is refused.
- The catalog never stores raw values. Identities are keyed by `HMAC(secret, kind \0 normalized)`, which
  is the same digest `PseudonymMapper` uses. Values are keyed by a separate HMAC, and source paths are
  stored only as an HMAC `path_id`.
- Normalization is exactly `identity._normalize`: NFKC, casefold, strip and collapse whitespace.
- Pseudonyms come from `PseudonymMapper`. Its protected JSON store is
  `<db>.pseudonyms.json` by default.
- The secret fingerprint is stored. A catalog or store opened with a different secret is rejected. A
  catalog whose pseudonym store is missing entries is also rejected.
- `location` and `reason` must be short printable ASCII. They may not contain the observed value.
- Reports, `repr` and error messages contain only truncated digests, pseudonyms, counts, locations and
  reasons.

## No-fuzzy-merge policy

- Two values share a pseudonym only if they normalize identically within the same kind.
- The catalog never infers that values belong to the same entity. That includes similar spellings,
  initials, substrings and edit distance.
- Values are linked only through `assert_alias(kind, canonical, alias)`. This is an explicit caller
  assertion between two exact identities of the same kind. The alias then resolves to the canonical
  pseudonym.
- The catalog rejects any assertion that contradicts an existing one: re-pointing an alias, creating a
  cycle, or aliasing an identity that is already canonical for others. The rejection is recorded in
  `conflicts` and the existing links are left unchanged.
- If the same value is confirmed under more than one kind, the catalog reports it as an ambiguity. It
  does not resolve it.
- Discovery candidates (`confirmed=False`) never get a pseudonym. They are listed under
  `unresolved_candidates` until the exact value is confirmed as an identity.

## Atomicity and resume

- Every mutating call runs as a single `BEGIN IMMEDIATE` transaction. Any invalid item or exception
  rolls back the whole batch.
- Each observation has a deterministic key built from the kind, value, source sha256, path, location
  and confirmed/candidate status. Re-recording an observation after a restart is a no-op.
- The pseudonym store may keep entries from a rolled-back transaction. This is harmless, because the
  same secret and identity always produce the same pseudonym.

## Report

`IdentityCatalog.report()` returns JSON-serializable data with these keys: `summary`, `identities`
(kind, pseudonym, `alias_of`, observation count), `unresolved_candidates` (candidate kinds and
locations by source hash and path id), `ambiguities` and `conflicts`.

## Phase 5C: approvals and the two-pass workflow

Schema v2 adds an `approvals` table (one row per exact `(kind, normalized value)` key: `approved`,
`rejected`, `review` or `conflict`, with `source` = `manual`|`auto` and an ASCII reason) and an append-only
`decisions` log. Still digests only; raw values are never stored.

- `decide/approve/reject/mark_review(kind, value, ...)` affect only the exact value given. Repeating a
  decision is a no-op. A value may be approved under only one kind; a second kind becomes `conflict` and
  records a `kind_conflict`. `auto` decisions never override an existing decision, and auto approval is
  blocked when the value is rejected or in conflict under any kind. Manual decisions may override.
- `approve_alias(kind, canonical, alias)` = `assert_alias` + approve both; contradictions approve nothing.
- `approved_pseudonym(raw_value)` takes a raw current candidate at transform time and returns
  `(kind, pseudonym)` only for an approved exact normalized value (following aliases).

`twopass.py`:

1. `discover_batch(cat, [(path, rel)], limit=..., max_bytes=...)`: format dispatch (PDF/DXF/STEP, and
   PDF/DXF/STEP members of bounded ZIPs, nested included, as `archive.zip!member`). Candidates stay in
   memory and are recorded with source sha256/path context. Each file is its own transaction, so a rerun
   after interruption is idempotent.
2. Auto policy (`auto_decision`): approve only `person_or_org`, `high` confidence, from PDF Info
   `/Author` or STEP `FILE_NAME` author/organization. Everything else (file_name, project, part,
   description, text_label_value, path_component, software, unknown, DXF `$LASTSAVEDBY`) becomes `review`.
   Auto approval is typed for structurally explicit STEP `FILE_NAME.author` (`person`) and `organization` (`org`) fields. PDF `/Author` remains ambiguous and requires explicit typed review; it is never silently assigned a person or organization kind.
3. `build_identity_map(cat, candidates)` returns an in-memory `IdentityMap` (strings only) of approved
   exact values. `transform_file` re-discovers the current file, builds the map and calls the unchanged
   transformers. STEP stays FILE_NAME-header only; the DATA section is analysis-only.

CLI: `python -m machina_deid catalog {discover|report|approve|reject|review|alias|transform} --catalog DB
[--secret-file F]`. Values for approve/reject/review/alias are read from stdin, one per line, and are never
passed in argv.

## Phase 5D: STEP DATA `ORGANIZATION.name`

`step_data_transform.py` is the only STEP DATA rewrite. Schema (ISO 10303-41):

```
ENTITY organization;
  id          : OPTIONAL identifier;  -- parameter 1 (index 0), never touched
  name        : label;                -- parameter 2 (index 1), the only value replaced
  description : OPTIONAL text;        -- parameter 3 (index 2), never touched
END_ENTITY;
```

Organization roles are not attributes of `ORGANIZATION`. They are carried by `ORGANIZATION_ROLE` and the
`*_ORGANIZATION_ASSIGNMENT` entities, which are not modified.

- Scope: the instance must be simple (not a complex entity), its type must be exactly `ORGANIZATION`, it
  must have exactly 3 top-level parameters, and `name` must be a plain string literal. Records are
  located with the structural `step_analysis` scanner. No regex runs over DATA.
- Match: the literal is decoded (`''`, `\\`, `\X\`, `\X2\`, `\X4\`, `\S\`, and raw UTF-8 bytes). It is
  replaced only if the decoded value is exactly (character for character) a key of the in-memory org map.
  Undecodable names are reported as unmodifiable and left unchanged.
- Approval: discovery emits kind `org_name` (`step:DATA.ORGANIZATION.name[k]`, medium). Policy always
  sends it to `review`, and placeholders (`UNKNOWN`, `NONE`, ...) are dropped. `twopass.build_org_map`
  includes a value only if the catalog has it approved under the typed `org` kind. A value approved under
  another kind (for example `text`) is not replaced in DATA and is counted as `residual_approved`. The
  catalog's notion of "the same identity" is its normalization (NFKC/casefold/whitespace). The splice
  itself requires the exact string the current file contains.
- Pipeline: `transform_file` runs the FILE_NAME header stage (untyped map) first, then the DATA stage. ZIP
  members still get the header stage only.
- Verification (fail-closed, nothing written on failure): the output is reparsed. The header bytes, entity
  id/type/arity/reference lists and per-entity string counts must be identical. The numeric-token sequence
  outside literals must be identical. All bytes outside the edited literals must be identical, and each
  edited literal must decode to the planned pseudonym. The write is atomic, and running the transform
  again on its own output is a no-op.
- **Limitation:** no geometry/topology validation is performed, because no trusted geometry kernel is
  available. Geometric validity is inferred only from the structural and numeric invariants above.

## Phase 5G: private candidate review

`scripts/private_review.py` is a local-only review tool; all its output lives under
`/srv/new2/machina-deid/work/<bench>/review/<name>/` (directories 0700, files 0600) and never in Git.

* `report --bench B --catalog C` checks the frozen `selection.json`/`checksums.json` against the files, runs
  discovery again on the selected files, and groups candidates by the catalog's normalized value key.
  It does not change the catalog. `private-review.json` is the only file that holds raw values;
  `public-summary.json` and the default stdout contain only opaque 16-hex digests and counts.
  Pass `--show-private` to print raw values to the terminal.
* `decide --bench B --catalog C --report <private-review.json>` records explicit, typed manual decisions
  through `IdentityCatalog.decide(source="manual")`. Identities are referred to by digest, never by raw value:
  either pass `--digest D --kind K --decision approve|reject|review --reason "..."`, or use `--stdin` with
  lines of the form `<digest> <kind> <decision> <reason>`.

Reports are authenticated with the catalog secret and tied to the benchmark, the frozen files,
and the catalog path. Regenerate reports created by the earlier unsigned workflow before deciding.
The report opens the catalog read-only. Decision input is checked as a batch before writes, and reasons
must use generic descriptions without candidate values or private paths.

### Verified transformation counts

The Phase 5E benchmark transformation section and the catalog transform output report these counts:

- PDF visible-text replacements are counted when the original disappears and the replacement is
  extracted on the same page. Metadata counts each cleared Info field or parsed XMP text or
  attribute field that contained a mapped identity once, even when the identity occurs repeatedly
  or multiple identities share the field. Attachments, forms, and existing annotations have no
  supported identity rewrite and report zero changes.
- STEP counts validated FILE_NAME header and typed DATA organization changes separately. DXF
  counts edited entity text and header fields verified on readback, once per field.
- ZIP and nested ZIP counts are subtotals of validated leaf-member changes. Those same changes
  contribute once to their format category and once to the total, regardless of nesting depth.
- `total_verified_identity_fields_changed` and the legacy `fields_replaced` field use the same
  total. Failed validation contributes zero. Residuals are reported separately, and a validated
  change alone does not establish that the output is fully clean.

The frozen Phase 5E sample includes the previously approved metadata-only PDF. The existing
approval produced two verified metadata changes and zero visible-text replacements. Inspection
of the ten selected neutral and mesh files did not establish a metadata field with both a reviewed
identity and a verified geometry-preserving rewrite. The redacted inspection report is stored
with the private benchmark results.
