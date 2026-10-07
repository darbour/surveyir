# Qualtrics fixtures

Real `.qsf` exports from the Twin-2K-500 mega-study replications
(<https://github.com/TianyiPeng/Twin-2K-500-Mega-Study>, Apache-2.0), used as
the committed regression corpus for the Qualtrics loader. Each
`<study>.columns.json` holds only the two header rows (column name and
ImportId) of that study's Qualtrics CSV export. It contains no response data,
and it is the oracle for `surveyir.response_columns`.

`tests/fixtures/validation/twin.json` holds aggregates only (arm counts, question
counts, logic-replay tallies), computed from the same studies' response files by
`scripts/validate_runtime.py`. No response rows are stored here.

A much larger corpus of public QSFs and export headers is listed in
`corpus/manifest.json` and downloaded on demand with
`scripts/fetch_corpus.py`. Those files are not redistributed here.
