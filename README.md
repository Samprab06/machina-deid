# Machina CAD De-ID

MIT-licensed, resumable, format-aware pseudonymization pipeline for the Builders Machina CAD De-ID challenge.

## Confirmed requirements

- Current challenge page: 188,722 files, about 139.7 GB; current page says 20 scored attempts and Builders private storage.
- Current page is authoritative over the linked PDF where they conflict.
- Scored output is `corpus/`; each attempt also includes `code/` and `README.txt`.
- Preserve geometry, dimensions, tolerances, units, materials, configurations, BOMs, references, and usable native CAD formats.
- Replace identities consistently across visible text, filenames, metadata, embedded previews/attachments, native properties, and archives.
- Do not silently discard duplicates, empty files, invalid files, or unsupported formats; record neutral exceptions.

## Environment findings

- Linux 6.12 on aarch64; 2 CPU cores; 10 GiB RAM; no GPU.
- Root filesystem: 42 GiB free.
- `/srv/new2`: 100 GiB free, case-sensitive XFS, currently root-owned and not writable by `opc`.
- Full corpus is approximately 139.7 GB before extraction, intermediates, and output; full processing is blocked until more writable storage is available.
- The first 100 manifest entries are approximately 427 MB.

## Official materials

- `manifest.tsv` — downloaded manifest.
- `get-input.sh` — official downloader.
- `challenge.pdf` — downloaded brief; its attempt/storage instructions conflict with the current page and are not used for those points.
- `upload.py` — downloaded official helper; inspect before any upload.

## Development layout

- `src/` — inventory, identity, transforms, validation, orchestration.
- `scripts/` — operational utilities.
- `tests/` — unit/integration tests.
- `input/` — private sample/raw input; ignored by Git.
- `work/` — temporary/checkpoint state; ignored.
- `output/` and `attempts/` — generated outputs; ignored.

## Modules

- `src/machina_deid/identity.py` — HMAC-SHA256 pseudonym mapper (`PseudonymMapper`, `load_secret`). The secret comes from an explicit file path or the `MACHINA_DEID_SECRET` environment variable (min 16 bytes); there is no default. The persisted JSON map holds only HMAC digests and pseudonyms, never raw identities or the secret. Keep the secret file outside the repo (e.g. `chmod 600`).
- `src/machina_deid/catalog.py` — persistent SQLite identity catalog (Phase 5A): HMAC-keyed identities and observations, explicit aliases only (no fuzzy merging), redacted reports. See `docs/identity-catalog.md`.
- `src/machina_deid/production.py` + `scripts/run_full_corpus.py` — parallel, resumable full-corpus runner (frozen catalog, per-file staging, parent-written checkpoints, dc3da69 migration). Runbook: `docs/production-runner.md`.
- `src/machina_deid/classify.py` — conservative extension + magic-byte classifier; reads at most 16 bytes per file, and reports `verified`, `unverified`, `mismatch`, `empty`, or `unknown`. It never implies a rewrite is supported.

## Local test and safety commands

```sh
cd /home/opc/machina-deid
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
python -m pytest
python -m compileall -q src tests scripts
# safety: confirm no secrets or private data are tracked or staged
git status --short
git check-ignore -v input work output attempts
```

Tests use only synthetic in-memory/temp-dir data; they never touch `input/`, `work/`, `output/`, or `attempts/`.

No scored submission is made without an explicit approval gate showing counts, bytes, validation results, risks, destination, and remaining attempts.
