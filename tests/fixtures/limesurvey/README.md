# LimeSurvey fixtures

**Hand-made** (written by `scripts/make_limesurvey_fixtures.py`; regenerate with
`uv run python scripts/make_limesurvey_fixtures.py`):

| File | What it covers |
|---|---|
| `types_v3.lss`, `types_v6.lss`, `types_v7.lss` | One question of every LimeSurvey type, in English and German, written in the 2.x/3.x (inline text), 4.x–6.x (`*_l10ns` tables) and 7.x (ranking subquestions) layouts. All three load to the same IR. |
| `logic.lss` | Relevance (lowered and kept as expressions), same-page relevance, group relevance, array filters, subquestion relevance, piping, participant attributes, a hidden equation, validation equations, randomization groups, question random groups, a quota. |
| `conditions.lss` | Legacy condition rows: scenarios, multiple choice, "not answered", numbers, a regular expression, a participant attribute. Question-by-question format. |
| `responses.lsa`, `responses.csv` | `types_v6.lss` with three responses written by hand, as LimeSurvey archives them (`.lsr`, SGQA field names) and as a CSV export with question-code headings (`;` separated). |

**Third party:** `citric/` holds the example exports of
[citric](https://github.com/edgarrmondragon/citric) (commit `eeb9fb2`), MIT
licensed, © 2019 Edgar Ramírez Mondragón; see `citric/LICENSE`.

LimeSurvey's own demo and test surveys (GPL-2.0-or-later) are not committed:
`corpus/manifest.json` lists them and `scripts/fetch_corpus.py --only limesurvey`
downloads them for `tests/test_limesurvey_corpus.py`.
