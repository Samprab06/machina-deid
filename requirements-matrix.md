# Requirements Matrix (verified 2026-11-20)

| Area | Confirmed requirement | Evidence |
|---|---|---|
| Input | 188,722 files, ~139.7 GB; PDFs, drawings, native CAD, neutral CAD, images, archives | Current challenge page + manifest |
| Output | `attempt-N/corpus/`, `code/`, `README.txt` | Current challenge page |
| Identity removal | Names, organizations, contact info, domains, accounts, addresses, logos, project/part names and numbers; consistent pseudonyms | Current challenge page + challenge PDF |
| Preservation | Geometry, dimensions, tolerances, units, materials, configs, BOMs, references, native usability | Current challenge page + challenge PDF |
| Robustness | Duplicates, empty, invalid, unsupported inputs must be recorded, not silently dropped | Current challenge page |
| Limits | Max 400,000 corpus objects; files over 2 GB skipped; scorer download/read limits | Current challenge page |
| Attempts | 20 scored attempts, 2 submissions/hour | Current challenge page; conflicts with PDF's 3 attempts |
| Storage | Builders private folder via official upload helper; AWS alternative only with approval | Current challenge page |
| License | MIT | General Buildathon rules |
| Local blocker | 42 GiB root free + 100 GiB root-owned disk; insufficient writable capacity for full corpus + intermediates/output | Live system inspection |

## Open decisions

1. Make `/srv/new2` writable for `opc`, or provision additional storage; ask before changing storage/permissions.
2. Obtain any signed-in upload token/team destination only at submission time; never invent it.
3. Validate native SolidWorks/Inventor transformation capabilities experimentally; do not claim support from filename/text inspection alone.
