# OLE core (branch `claude/machina-ole-core`): integration notes

New files only: `src/machina_deid/ole_transform.py`, `src/machina_deid/ole_cli.py`,
`tests/ole_helpers.py`, `tests/test_ole_transform.py`. No dependencies were added, because it has its own CFB reader.
Production, twopass and shared docs are unchanged.

## Evidence basis (local aggregate, no values)
- 55/55 signature-positive SolidWorks had SummaryInformation PIDSI_LASTAUTHOR (pid 8). Inventor had none.
- 185/185 UTF-16LE matches that follow an MFC Unicode CString prefix `FF FE FF <len>` had a matching length.
- 815/1000 cross-stream UTF-16LE matches were adjacent to a path or contact separator (`\ / @ .`).

## What is written (verified, equal length, in place)
| kind | rule |
|---|---|
| `property` | Exact typed parse of the FMTID_SummaryInformation section; VT_LPSTR (codepage pid 1) or VT_LPWSTR; value bytes before the terminator. Default pids `(8,)`; pass `pids=(4, 8)` to include AUTHOR. |
| `cstring` | The MFC Unicode CString prefix (u8/u16/u32 forms) declares exactly `len(value)` chars, and the content equals the value. |
| `path_component` | A whole token in a printable UTF-16LE run. Each neighbour is a separator, `:` or the run edge, and the run contains a separator. |

Candidates come only from parsed property values that are in the caller's mapping. Other mapping keys are ignored.
Streams whose names contain `preview` or `thumbnail` are never written. Other byte classes are only counted and never written:
- `raw_utf16`: bare substrings, wrong CString length and preview hits.
- `raw_ascii`.

The CFB header, FAT, MiniFAT, DIFAT, directory, sizes and compressed payloads stay byte-identical.

## Alias contract
The caller supplies `{original: alias}`, and each alias must have the same character length as its original. The encoded byte length must also match in every record encoding. If not, the whole file is refused (`alias_length_mismatch` / `alias_unencodable`) and the original bytes are returned. `equal_length_alias(original, key)` is a deterministic HMAC helper that also works for 1 to 3 character accounts. Integration should give it the catalog's existing pseudonym instead, when that pseudonym has the same length.

## Verification
`transform` always runs `inspect_result` and refuses its own output on failure. `inspect_result` does the following:
- re-parses both files from bytes;
- requires the same length, directory, FAT and MiniFAT;
- requires that every changed byte lies inside a recorded write span;
- requires that every write actually changed its bytes;
- requires no verified-kind residue of a transformed value.

It reports the remaining informative raw hits as a count.

## CLI (redacted: counts, lengths and kinds only)
```
python -m machina_deid.ole_cli inspect FILE...
python -m machina_deid.ole_cli transform IN OUT --map map.json | --alias-key-file KEY
```

## Integration plan for the main session
1. Route CFB-signature files (`ot.is_cfb`) through `ot.transform(data, mapping)`, where the mapping is approved person/account values mapped to existing pseudonyms. Treat `reason != None` as passthrough plus a residual record.
2. Count `Result.counts` by kind for the residual diagnostics. Do not log values.

## Known gaps (checkpoint)
- There is no dedicated parser yet for the SolidWorks `LastSavedBy` stream record. It is covered only when it uses the MFC CString or path form.
- ANSI/MBCS CStrings are reported as `raw_ascii` and not written.
- DocumentSummaryInformation and user-defined properties are not parsed.
- Inventor is detected but has no target, because LASTAUTHOR is absent.
- Tests use only flat synthetic CFB v3 files (512-byte sectors). The parser supports 4096-byte sectors and nested storages, but no fixture covers them yet.
