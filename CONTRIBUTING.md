# Contributing

```bash
uv sync
uv run pytest            # unit tests + invariants over every fixture survey
uv run ruff check src tests scripts examples && uv run ruff format src tests scripts examples
uv run pyright
```

**External corpus.** `GITHUB_TOKEN=... uv run python scripts/fetch_corpus.py`
downloads ~860 public QSFs and ~2,000 export headers listed in
`corpus/manifest.json`. `tests/test_external_corpus.py` and
`tests/test_external_columns.py` then check every file. `tests/known_gaps.json` is
the allowlist of Qualtrics types that may still load as `unsupported`; it should
only shrink.

**Found a .qsf that loads wrong?** The most useful contribution is the file
itself (with permission) in `tests/fixtures/qualtrics/`. Run
`surveyir inspect file.qsf --extras` to see diagnostics and untyped keys.

**Changing the IR?** Bump `SCHEMA_VERSION` in `src/surveyir/model/base.py` when
the change is visible to consumers. Then regenerate the published artifacts,
which CI checks:

```bash
uv run surveyir schema -o schema/surveyir.schema.json
uv run python scripts/parity_report.py   # with the external corpus downloaded
```

CI checks that the schema is up to date. It does not diff `docs/parity.md`,
because the report depends on the external corpus. Regenerate it by hand when
coverage changes.

**Adding a question type or flow element:** add the model in `src/surveyir/model`,
map it in `src/surveyir/loaders/qualtrics`, add a synthetic test in
`tests/test_questions.py`, and render it in the Markdown exporter.
