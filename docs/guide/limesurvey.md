# LimeSurvey

surveyir reads LimeSurvey surveys as well as Qualtrics ones. A LimeSurvey survey
becomes the same IR, so everything else works unchanged:

- the exporters;
- the experimental design;
- the simulator;
- response columns, which follow LimeSurvey's own export;
- replay of real responses.

This page covers what is LimeSurvey-specific. Coverage is measured in
[LimeSurvey parity](../parity_limesurvey.md).

## Loading

Every LimeSurvey export of a survey's structure loads:

| File | What it is |
|---|---|
| `.lss` | The survey structure |
| `.lsa` | A survey archive: the `.lss` plus, when exported, its responses (`.lsr`), participants and timings |
| `.lsg` | One question group |
| `.lsq` | One question |
| `.txt` | The tab-separated structure export. Pass `format="limesurvey"`, since `.txt` is ambiguous |

All three generations of the file format load to the same IR:

- **2.x and 3.x:** text on the rows, one row per language.
- **4.x to 6.x:** text in `*_l10ns` tables.
- **7.x:** ranking items as subquestions.

```python
import surveyir

survey = surveyir.load("tests/fixtures/limesurvey/logic.lss")
print(survey.name, survey.source.format, survey.source.format_version)
print(len(survey.questions), "questions in", list(survey.blocks))
```

```text
Logic limesurvey.lss 498
22 questions in ['welcome', 'G10', 'G11', 'G12', 'G13', 'G14']
```

Question groups become blocks (`G<gid>`). Question codes are the question ids, and
answer and subquestion codes are the choice ids, so ids match LimeSurvey's data.
The survey's format decides the pages:

- **Group by group** (the default): each group is one page.
- **Question by question:** each question is its own page.
- **All in one:** administered group by group, with a diagnostic.

The welcome page and each group's name and description are shown as LimeSurvey
shows them. They are text displays marked
`extras["limesurvey"]["synthetic"]`, and they have no data column.

## Question types

Every LimeSurvey type maps onto an existing IR kind. The conventions are the same
as the Qualtrics loader's for the same shapes.

| LimeSurvey | IR |
|---|---|
| List (radio, dropdown, with comment), 5 point, Gender, Yes/No, Language | `choice`, single |
| Multiple choice (with comments) | `choice`, multiple |
| Arrays (flexible, by column, 5/10 point, Yes/Uncertain/No, Increase/Same/Decrease) | `matrix` |
| Array dual scale | `side_by_side`: one matrix per scale |
| Array (Numbers), Array (Texts) | `matrix` in text mode, rows × columns |
| Multiple numerical input, multiple short text | `text_entry`, form mode |
| Short, long, huge free text; Numerical input; Date | `text_entry` (numbers and dates validated as such) |
| Ranking | `rank_order` |
| Text display | `descriptive` |
| Equation | `equation`: computed when its page is submitted, recorded like an answer |
| File upload | `file_upload` |

Choice ids follow LimeSurvey's codes:

- **Other option:** a list's "Other" is `-oth-`; a multiple choice's is `other`.
- **No answer:** an optional question shows a "No answer" option when the survey
  offers one. It is a choice (or matrix column) with the empty id.
- **Help text:** shown under the question, as part of its text.

```python
q = survey.questions["pets"]
print(q.kind, q.multiple, [c.id for c in q.choices])
```

```text
choice True ['SQ001', 'SQ002', 'SQ003', 'other']
```

## Logic

LimeSurvey's logic is written in ExpressionScript:

- relevance equations on questions, groups and subquestions;
- validation equations;
- `{...}` piping.

The loader parses ExpressionScript with LimeSurvey's own grammar. It expands
`self` and `that` as LimeSurvey does, and converts legacy condition rows the way
LimeSurvey converts them.

A condition becomes `Comparison` nodes where that gives exactly LimeSurvey's
result. Otherwise it is kept as an `Expression` leaf with its source and syntax
tree. ExpressionScript fails a whole expression that reads a question hidden by its
own logic (unless `.NAOK` is used). The lowered form keeps that by requiring the
question to be `relevant`.

```python
from surveyir import describe_condition

for qid in ("dogname", "why", "hello"):
    q = survey.questions[qid]
    cond = q.display_logic or q.in_page_display_logic
    print(qid, "|", describe_condition(cond, survey), "|", type(cond).__name__)
```

```text
dogname | pets is selected | Comparison
why | likes = A1 | Comparison
hello | `age >= 18` | Expression
```

`why` depends on a question on its own page, so it is in-page display logic: it
appears as that question is answered, as in LimeSurvey. The rest of the logic maps
as follows:

| LimeSurvey | IR |
|---|---|
| Group relevance | A flow branch around the group's block |
| Groups sharing a randomization group | A flow randomizer |
| Questions sharing a `random_group` | Shuffled among their own positions |
| `array_filter`, subquestion relevance | Choice display logic |
| Quotas | `Quota` conditions, checked after each page that holds a quota question |
| Participant attributes (`TOKEN:ATTR`) | Recipient fields declared at the start of the flow. A run must supply them (`implementations={"embedded": {"TOKEN:FIRSTNAME": "Ann"}}`) or a strict run stops |

## Simulating

The runtime evaluates ExpressionScript the way LimeSurvey's Expression Manager
does:

- values are strings;
- `==` is PHP's loose equality;
- ordering comparisons between a number and an empty answer are false;
- `+` concatenates non-numbers;
- about 80 functions are implemented.

Equations take their value when their page is submitted. Piped text reflects
earlier answers on the same page by the time the respondent answers.

```python
from surveyir import RandomAnswerer, Simulator

sim = Simulator(survey, seed=1, strict=False,
                implementations={"embedded": {"TOKEN:FIRSTNAME": "Ann"}})
run = sim.respondent(RandomAnswerer(seed=2))
print(run.finished, [a.code for a in run.state.approximations])
```

```text
True []
```

Random answers do not read validation tips. They can therefore break a validation
equation such as `budget`'s ("the parts must add up to 10"), and a strict run then
stops with `answer.invalid`. A respondent that reads the displayed tip, which
`Display.validation["message"]` carries, would not break it.

What the runtime cannot reproduce is recorded with a code, as for Qualtrics
(`expression.unknown_function`, `expression.clock`, ...). `surveyir check
survey.lss` lists every such gap before a run. The
[parity page](../parity_limesurvey.md) lists the known differences.

## Data columns

`response_columns`, `RespondentRun.row` and `write_responses_csv` follow
LimeSurvey's export for a LimeSurvey survey. The default headings are question
codes, as in a CSV export with "question code" headings.

```python
cols = surveyir.response_columns(survey)
print([c.name for c in cols][:8])
print([c.name for c in cols if c.question_id == "petrate"])
```

```text
['id', 'submitdate', 'lastpage', 'startlanguage', 'seed', 'token', 'age', 'consent']
['petrate[SQ001]', 'petrate[SQ002]', 'petrate[SQ003]']
```

`LimeColumnOptions` picks other headings:

- `heading="em"`: ExpressionScript names such as `petrate_SQ001`.
- `heading="field"`: database field names, as in `.lsr` response files. These are
  SGQA codes (`222222X11X502SQ001`) before LimeSurvey 7, and `Q502_S5021` from 7 on.

`write_lsr` writes a response file LimeSurvey can import.

```python
from surveyir.columns_limesurvey import LimeColumnOptions

for naming in ("sgqa", "qid"):
    opts = LimeColumnOptions(heading="field", naming=naming)
    print([c.name for c in surveyir.response_columns(survey, options=opts)
           if c.question_id == "petrate"])
```

```text
['222222X11X502SQ001', '222222X11X502SQ002', '222222X11X502SQ003']
['Q502_S5021', 'Q502_S5022', 'Q502_S5023']
```

## Real responses

`read_responses` reads responses into replay recordings. It takes an `.lsa`
archive, an `.lsr` file, or a CSV export (comma or semicolon separated, any
heading). Replaying a recording walks the survey with the recorded answers, so its
logic is evaluated as the respondent went.

LimeSurvey does not export the order it presented things in, only the seed that
drove it. Replays therefore draw orders, listed as fallbacks.

```python
from surveyir.columns_limesurvey import read_responses
from surveyir.runtime.replay import Replayer

types = surveyir.load("tests/fixtures/limesurvey/types_v6.lss")
recordings = read_responses("tests/fixtures/limesurvey/responses.lsa", types)
replayer = Replayer(types, strict=False, fallback_kinds=("block", "choices", "columns", "flow"))
again = replayer.respondent(recordings[0].orders, recordings[0].answers)
print(len(recordings), again.answers[("rank", None)].value, again.answers[("score", None)].value)
```

```text
3 {'R3': 1, 'R1': 2, 'R2': 3} 37
```
