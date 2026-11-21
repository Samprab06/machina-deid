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
   Auto approvals use kind `text` (`Identity-...`) because person and org cannot be told apart from these
   fields, so the same string gets one pseudonym across formats.
3. `build_identity_map(cat, candidates)` returns an in-memory `IdentityMap` (strings only) of approved
   exact values. `transform_file` re-discovers the current file, builds the map and calls the unchanged
   transformers. STEP stays FILE_NAME-header only; the DATA section is analysis-only.

CLI: `python -m machina_deid catalog {discover|report|approve|reject|review|alias|transform} --catalog DB
[--secret-file F]`. Values for approve/reject/review/alias are read from stdin, one per line, and are never
passed in argv.
