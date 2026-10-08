# Exporting

An exporter turns a `Survey` into another format. Three ship with surveyir:

```python
import surveyir

for name, cls in surveyir.available_exporters().items():
    print(f"{name:<9} {cls.summary}")
```

```text
json      The surveyir IR as JSON (lossless unless compact=True).
markdown  Readable Markdown of the flow and every question (good LLM context).
codebook  CSV codebook: every response column with labels (Qualtrics naming).
```

`surveyir.export(survey, name, **options)` runs one. `get_exporter(name, **options)`
returns a configured exporter you can reuse, and its `write(survey, path)` writes
a file. Unknown options raise `TypeError` instead of being ignored.

```python
survey = surveyir.load_qsf("tests/fixtures/qualtrics/junk_fees.qsf")
```

## JSON: the internal format

The JSON exporter writes the full IR. Load it back with `Survey.from_json`; the
round trip is exact.

```python
text = surveyir.export(survey, "json")
again = surveyir.Survey.from_json(text)
print(again == survey, f"{len(text):,} characters")
```

```text
True 609,230 characters
```

`compact=True` drops `extras` (untyped source keys), raw HTML and survey options.
The result is much smaller but no longer lossless:

```python
compact = surveyir.export(survey, "json", compact=True, indent=None)
print(f"{len(compact):,} characters")
```

```text
313,529 characters
```

The JSON validates against `schema/surveyir.schema.json` (`surveyir schema`
regenerates it), so tools in other languages can read it.

## Markdown: a readable instrument

The Markdown exporter shows the flow outline and the experimental design, then
every block and question in order, with codes, logic and randomization written
out. It works well as context for an LLM.

```python
md = surveyir.export(survey, "markdown")
print(md[:md.index("- Randomize: show 1 of 3")])
```

```text
# Junk Fees Quiz - April 2025 - Updated/final

_72 questions in 9 blocks · source: qualtrics.qsf_

## Flow

- Set embedded data: `PROLIFIC_PID` (recipient), `STUDY_ID` (recipient), `SESSION_ID` (recipient)
- Block: **Intro**
- Randomize: show 6 of 6 in random order
  - Group: Hotel
```

Options:

| option | default | effect |
|---|---|---|
| `language` | `None` | render a translation (e.g. `"ES"`) where one exists |
| `include_logic` | `True` | show display, skip and branch logic |
| `include_codes` | `True` | show recode values next to choices |
| `include_unused` | `False` | also render blocks the flow never reaches |
| `include_hidden` | `False` | include timing, meta-info and captcha questions |

## Codebook: one row per data column

The codebook is a CSV with one row per column of the survey's response data,
using the same column names as a Qualtrics export. Each row has value labels:

```python
import csv, io

rows = list(csv.DictReader(io.StringIO(surveyir.export(survey, "codebook"))))
for row in rows[4:7]:
    print(row["column"], "|", row["part"], "|", row["value_labels"][:70])
```

```text
Hotel.MCQ | response | 1=${e://Field/correct.answer} | 2=${e://Field/wrong.answer1} | 3=${e:/
Hotel.MCQ_DO_1 | display_order |
Hotel.MCQ_DO_2 | display_order |
```

## Writing an exporter

Subclass `Exporter`, give it a `name`, and implement `export`. Options are
constructor keyword arguments:

```python
from surveyir import Exporter


class QuestionList(Exporter):
    name = "question-list"
    extension = ".txt"
    summary = "One line per question: tag, kind, text"

    def __init__(self, *, width: int = 50):
        self.width = width

    def export(self, survey):
        return "\n".join(
            f"{q.export_tag}\t{q.kind}\t{q.text.plain[: self.width]}"
            for q in survey.iter_questions(responses_only=True)
        )


print(QuestionList(width=40).export(survey).splitlines()[0])
```

```text
Hotel.MCQ	choice	Which of the following do you think best
```

To make it available as `surveyir convert -t question-list` and
`surveyir.export(survey, "question-list")`, register it in your package's
`pyproject.toml`:

```toml
[project.entry-points."surveyir.exporters"]
question-list = "my_package.exporters:QuestionList"
```

Loaders for other survey platforms plug in the same way, under
`surveyir.loaders`.
