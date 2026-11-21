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
