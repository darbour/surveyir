# Quickstart

This walks through the core workflow on a real survey: the *Junk Fees* study
from the Twin-2K-500 mega-study, which ships with the repository as a test
fixture. Every example on this page is executed by the test suite, and the
output shown is what it actually prints.

## Install

surveyir is not on PyPI yet. Install it from a checkout:

```bash
git clone <repository-url> surveyir && cd surveyir
uv sync                  # or: pip install -e .
```

Run the examples with `uv run python` (or plain `python` in an activated
environment). Paths below are relative to the repository root.

## Load a survey

```python
import surveyir

survey = surveyir.load("tests/fixtures/qualtrics/junk_fees.qsf")
print(survey.name)
print(len(survey.questions), "questions in", len(survey.blocks), "blocks")
```

```text
Junk Fees Quiz - April 2025 - Updated/final
133 questions in 9 blocks
```

`surveyir.load` picks the loader from the file extension. `.qsf` files go to
the Qualtrics loader, which you can also call directly as
`surveyir.load_qsf(path)`.

## Look at the questions

Questions are typed: each has a `kind` (`choice`, `matrix`, `text_entry`,
`slider`, ...), an `export_tag`, and text with its HTML already converted to
plain text.

```python
for q in list(survey.iter_questions(responses_only=True))[:4]:
    print(f"{q.export_tag:<16} {q.kind:<8} {q.text.plain[:60]}")
```

```text
Hotel.MCQ        choice   Which of the following do you think best represents what ${e
Hotel.Surprised1 choice   How surprised are you by what is covered by ${e://Field/Hote
Hotel.Surprised2 choice   How surprised are you that hotels assess ${e://Field/Hotel}?
Hotel.Heard      choice   Have you ever heard of ${e://Field/Hotel} before seeing it i
```

`iter_questions` follows the survey flow. `responses_only=True` skips
descriptive text, timers and other questions that collect no answer.

Choice questions keep their choices in display order, with recode values:

```python
q = survey.question("Hotel.MCQ")       # by export tag or QID
print(q.multiple, q.layout, q.randomization.mode)
for choice in q.choices:
    print(choice.id, choice.recode, choice.text.plain)
```

```text
False vertical all
1 None ${e://Field/correct.answer}
2 None ${e://Field/wrong.answer1}
3 None ${e://Field/wrong.answer2}
4 None ${e://Field/wrong.answer3}
```

Note the `${e://Field/...}` references. That's Qualtrics *piped text*,
filled in from embedded data when the survey runs. surveyir keeps the
reference and parses it, so you decide what to substitute:

```python
print([(p.kind, p.name) for p in q.text.pipes])
print(q.text.render(lambda pipe: {"Hotel": "a resort fee"}.get(pipe.name)))
```

```text
[('embedded_data', 'Hotel')]
Which of the following do you think best represents what a resort fee is assessed for?
```

## Export

Exporters turn a survey into another format. `markdown` is a readable
version of the whole instrument, which is also good context for an LLM:

```python
md = surveyir.export(survey, "markdown")
start = md.index("#### `Hotel.MCQ`")
print(md[start:md.index("---", start)])
```

```text
#### `Hotel.MCQ` · single choice, vertical

> Which of the following do you think best represents what ${e://Field/Hotel} is assessed for?

_Response required. Choices shown in random order._

1. ${e://Field/correct.answer} `[1]`
2. ${e://Field/wrong.answer1} `[2]`
3. ${e://Field/wrong.answer2} `[3]`
4. ${e://Field/wrong.answer3} `[4]`
```

`json` writes the full internal format, and `codebook` writes one CSV row per
data column. See [Exporting](guide/exporting.md).

## Data columns

`response_columns` lists the columns a Qualtrics export of this survey would
have, with the same names and ImportIds. Simulated data can then be analyzed
with the same code as real data:

```python
columns = surveyir.response_columns(survey)
print(len(columns), "columns")
for c in columns[4:9]:
    print(c.name, c.import_object)
```

```text
148 columns
Hotel.MCQ {'ImportId': 'QID10'}
Hotel.MCQ_DO_1 {'ImportId': 'QID10_DO', 'choiceId': '1'}
Hotel.MCQ_DO_2 {'ImportId': 'QID10_DO', 'choiceId': '2'}
Hotel.MCQ_DO_3 {'ImportId': 'QID10_DO', 'choiceId': '3'}
Hotel.MCQ_DO_4 {'ImportId': 'QID10_DO', 'choiceId': '4'}
```

## Next steps

- [Loading surveys](guide/loading.md): diagnostics, strict mode, unsupported types.
- [Exporting](guide/exporting.md): every exporter, and writing your own.
- [Data columns](guide/data-columns.md): export layouts and reading real exports.
- [Experiments](guide/experiments.md): what a survey randomizes, and how.
- [Simulating respondents](guide/simulation.md): running people (or LLMs) through a survey.
- [Command line](cli.md).
