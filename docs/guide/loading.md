# Loading surveys

```python
import surveyir

survey = surveyir.load_qsf("tests/fixtures/qualtrics/junk_fees.qsf")
```

`load_qsf` accepts a path, an open file, or an already-parsed `dict`.
`surveyir.load(path)` does the same, choosing the loader by file extension (see
`surveyir formats`).

## What a survey contains

```python
print(survey.source.format, survey.source.format_version)
print("blocks:", [b.description for b in survey.blocks.values()][:5])
print("flow:", [n.type for n in survey.flow][:6])
```

```text
qualtrics.qsf 1.1.0
blocks: ['Intro', 'Hotel', 'ID', 'Rental Cars', 'Tickets']
flow: ['embedded_data', 'block', 'randomizer', 'block', 'end_survey']
```

- `survey.questions` and `survey.blocks` are keyed by Qualtrics ID, in file order.
- `survey.flow` is the tree that decides what a respondent sees.
- A block's `elements` are questions and page breaks, in presentation order.

```python
block = next(iter(survey.blocks.values()))
print(block.description, block.pages())
```

```text
Intro [['QID65'], ['QID64'], ['QID2', 'QID70']]
```

## Diagnostics

Real exports contain problems: references to deleted questions, logic
Qualtrics itself marks "Invalid Logic", question types surveyir doesn't model
yet. The loader never drops these silently. Each one becomes a `Diagnostic`:

The Junk Fees survey is clean, so it has none. *Accuracy Nudges* has several
questions that share an export tag, which matters because their data columns
would collide:

```python
print(survey.diagnostics)
nudges = surveyir.load_qsf("tests/fixtures/qualtrics/accuracy_nudges.qsf")
for d in nudges.diagnostics[:2]:
    print(d.level, d.code, "-", d.message)
```

```text
[]
info duplicate-export-tag - Export tag 'Fake1_RT' used by QID1201, QID1517, QID1530
info duplicate-export-tag - Export tag 'Fake2_RT' used by QID1205, QID1520, QID1533
```

Levels mean:

| level | meaning |
|---|---|
| `info` | worth knowing, nothing lost (e.g. two questions share an export tag) |
| `warning` | something could not be represented as typed IR, or the source file is defective |
| `error` | something could not be loaded at all |

Here is a survey that uses a question type surveyir doesn't model yet:

```python
doc = {
    "SurveyEntry": {"SurveyID": "SV_demo", "SurveyName": "Demo"},
    "SurveyElements": [
        {"Element": "BL", "Payload": [{"Type": "Default", "ID": "BL_1",
            "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}]}]},
        {"Element": "FL", "Payload": {"Type": "Root", "Flow": [
            {"Type": "Block", "ID": "BL_1", "FlowID": "FL_2"}]}},
        {"Element": "SQ", "Payload": {"QuestionID": "QID1", "QuestionType": "FileUpload",
            "Selector": "VideoCapture", "SubSelector": "VideoToText",
            "DataExportTag": "Q1", "QuestionText": "Record a short video"}},
    ],
}
demo = surveyir.load_qsf(doc)
q = demo.questions["QID1"]
print(q.kind, "|", demo.diagnostics[0].message)
print(sorted(q.extras))          # the full source payload, verbatim
```

```text
unsupported | Video Response question (FileUpload/VideoCapture/VideoToText) has no public example to model; kept verbatim in extras
['DataExportTag', 'QuestionID', 'QuestionText', 'QuestionType', 'Selector', 'SubSelector']
```

## Strict mode

`strict=True` raises `LoadError` on errors and on loader gaps, such as
unsupported types, unknown selectors and question-type plugins. Defects in the
source file itself (a dangling reference, a missing flow) don't raise, because
the survey still loads faithfully.

```python
try:
    surveyir.load_qsf(doc, strict=True)
except surveyir.LoadError as e:
    print("refused:", e.diagnostics[0].code)
```

```text
refused: unsupported-question
```

## Nothing is dropped: `extras`

Every source key without a typed field lands in that node's `extras`. Most are
editor and display settings:

```python
q = survey.question("Hotel.MCQ")
print(q.extras)
```

```text
{'DataVisibility': {'Private': False, 'Hidden': False}, 'Configuration': {'QuestionDescriptionOption': 'UseText'}}
```

To see which keys are untyped across many files, run
`surveyir inspect *.qsf --extras`. [docs/parity.md](../parity.md) shows the
result for the full test corpus.

## Logic

Display, branch, skip and validation logic all become one expression tree
(`And` / `Or` / `Comparison`). Qualtrics evaluates AND before OR, and the tree
already encodes that precedence. `describe_condition` renders a condition in
words:

```python
from surveyir.model import BranchNode

twins = surveyir.load_qsf("tests/fixtures/qualtrics/obedient_twins.qsf")
for node in twins.walk_flow():
    if isinstance(node, BranchNode):
        print(f"{node.id:<6} {surveyir.describe_condition(node.condition, twins)}")
```

```text
FL_66  embedded data `drop` = yes
FL_62  Q57 text does not contain use
FL_35  attitude value (choice 1) < 50
FL_36  attitude value (choice 1) > 50
FL_37  attitude value (choice 1) = 50
```

The `Q57 text` condition is a screener: respondents whose answer doesn't
contain "use" are sent to the end of the survey. The `attitude` conditions
assign which side of an argument a respondent sees.

To *evaluate* a condition for a given respondent, see
[Simulating respondents](simulation.md#evaluating-logic-directly).
