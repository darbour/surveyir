# surveyir

**An intermediate representation (IR) for survey instruments that keeps every
source key, with a Qualtrics `.qsf` loader, a simulator that records what each
respondent saw, and pluggable exporters.**

Survey platforms export instruments in formats built for their editors, not for
analysis or simulation. `surveyir` turns them into one typed, documented,
JSON-serializable structure: questions, choices, blocks, pages, the survey flow,
display/skip/branch logic, randomization, Loop & Merge, piped text and
translations. Any tool (an LLM respondent simulator, a codebook generator, an R
analysis pipeline, a survey renderer) can then work from that one structure.

```text
 .qsf ──► loader ──► Survey (IR) ──► exporters ──► JSON · Markdown · codebook CSV · your format
```

## Why

- **Nothing is silently dropped.** Every source key either maps to a typed
  field or is preserved verbatim in `extras`. Question types that aren't modeled
  yet load as `UnsupportedQuestion` with a diagnostic. They are never coerced
  into something else.
- **Logic is data.** Display, branch, skip and custom-validation logic all
  become one small expression tree (`And` / `Or` / `Comparison`), with
  Qualtrics' AND-before-OR precedence already resolved.
- **Tested against real surveys, not just the spec.** The loader runs over
  880 real QSFs (19 committed, 861 public files from GitHub). Every
  question type found in them has a typed model, from side-by-side and
  pick-group-rank to heat maps and drill-downs, and none produces an error.
  See [docs/parity.md](docs/parity.md).
- **Data columns match real exports.** `response_columns()` produces the exact
  column names and ImportIds of a Qualtrics CSV export, including Loop & Merge,
  side-by-side, carry-forward and display-order columns. Simulated responses can
  be analyzed with the same code as human data. In 1,875 real exports paired
  with their .qsf, 36,102 of 36,155 question columns (99.85%) are reproduced
  exactly, and 67,641 of 67,697 (99.92%) when metadata, score and
  display-order columns are counted too ([docs/parity.md](docs/parity.md)).
- **Language-neutral.** The IR is plain JSON with a published
  [JSON Schema](schema/surveyir.schema.json), so R, JavaScript and other tools
  can consume it without Python.

## What surveyir claims, and on what evidence

Three separate claims, which need different evidence:

1. **Information preserved.** The IR holds everything in the source file.
   Evidence: source accounting (every key is read into a typed field or kept
   verbatim in `extras`; `surveyir inspect --extras` lists the untyped ones) and
   exact IR → JSON → IR round trips, both checked on all 880 corpus files
   ([docs/parity.md](docs/parity.md)). There is no QSF writer yet, so a
   .qsf → IR → .qsf round trip is not checked.
2. **Instrument administered faithfully.** A simulated respondent is shown what
   the instrument specifies, in that order, and given only what was shown.
   Evidence: hand-written exact traces checked in CI
   (`tests/test_exact_traces.py`), strict execution (the default), which stops
   a run at any approximation that could change assignment, exposure, routing
   or outcome (gaps that affect none of these are recorded, not stopped), and
   consistency checks against observed responses (below). This holds only for strict runs: a run with
   `strict=False` or `allow=` records its approximations in `run.audit`, but
   can differ from what Qualtrics would show.
3. **Responses behaviorally valid.** Whether simulated answers resemble what
   people would answer. This is out of scope for the core: it depends on the
   respondent model and needs separate empirical validation against human data.
   surveyir provides the record such a validation needs, not the validation.

See [docs/design.md](docs/design.md#what-surveyir-claims) for details and
[docs/positioning.md](docs/positioning.md) for how this compares with related
tools.

## Install

Not on PyPI yet. Install from a checkout:

```bash
git clone <repository-url> surveyir && cd surveyir
uv sync                     # or: pip install -e .
```

Requires Python 3.10+. The only runtime dependency is `pydantic>=2`.

## Documentation

Start with the [quickstart](docs/quickstart.md), then the guides:
[loading](docs/guide/loading.md), [exporting](docs/guide/exporting.md),
[data columns](docs/guide/data-columns.md), [experiments](docs/guide/experiments.md),
[simulating respondents](docs/guide/simulation.md) and the
[command line](docs/cli.md). [Positioning](docs/positioning.md) compares
surveyir with related tools. Every example in the docs is run by the test suite,
and runnable scripts are in [`examples/`](examples).

## Quick start

```python
import surveyir

survey = surveyir.load("study.qsf")

for q in survey.iter_questions(responses_only=True):
    print(q.export_tag, q.kind, q.text.plain[:60])

# What does a respondent see, and when?
from surveyir.model import BranchNode
for node in survey.walk_flow():
    if isinstance(node, BranchNode):
        print("branch:", surveyir.describe_condition(node.condition, survey))

# Exports
print(surveyir.export(survey, "markdown"))       # readable instrument / LLM context
surveyir.get_exporter("codebook").write(survey, "codebook.csv")
survey.to_json()                                 # canonical IR

# Column layout for simulated data, identical to a Qualtrics export
columns = [c.name for c in surveyir.response_columns(survey)]
# ...or to an export made with non-default settings
opts = surveyir.ColumnOptions(split_multi_value=False, display_order="single")
columns = [c.name for c in surveyir.response_columns(survey, options=opts)]
```

Piped text is parsed, so simulators decide how to fill it in:

```python
q = survey.question("Hotel.MCQ")
q.text.plain   # 'Which of the following ... what ${e://Field/Hotel} is assessed for?'
q.text.render(lambda pipe: respondent.embedded.get(pipe.name))
```

## Experiments and simulation

`surveyir.design(survey)` describes the experiment: every flow randomizer as a
factor with its arms, each arm's nominal share, the embedded-data values it
assigns (usually how the condition is recorded), and which factors are crossed
or nested. It also covers question, choice and loop order randomization,
random values, and any randomization a .qsf cannot capture (JavaScript, web
services, library blocks).

```python
d = surveyir.design(survey)
print(d.summary())
# FL_6: exposure contrast, 1 of 2, evenly presented
#   - FL_7 (nominal share 0.5): absurd_1='A'
#   - FL_8 (nominal share 0.5): absurd_1='B'
cells, complete = d.cells()
```

`Simulator` walks respondents through the survey: randomizers (including
Qualtrics' "evenly present" counterbalancing, which depends on earlier
respondents), branches, display and skip logic, question/choice/loop order,
Loop & Merge, carry forward and piped text. Your answerer is called for each
question it shows. Each run becomes a row with the same columns as a real
Qualtrics export.

```python
from surveyir.runtime import Simulator

def answer(view, state):        # view.text has piped text filled in;
    return my_llm(view)         # view.choices are in the order shown

sim = Simulator(survey, seed=1)
runs = sim.run(1000, answer)
rows = [r.row(survey) for r in runs]
```

The runtime was compared with 13,879 real respondents in 19 studies
(`tests/test_runtime_validation.py`, `scripts/validate_runtime.py`):

- **Randomizers:** 56 flow randomizers (159 arms) were compared: arm names, arms
  per respondent and arm frequencies all match the real data. The other 25
  randomizers had fewer than 50 real respondents each. Blocks with randomized
  question order show the same number of questions.
- **Consistency checks against observed responses:** each finished
  respondent's recorded embedded data and answers are fed to the logic
  evaluator. A violation is a question answered although the evaluator predicts
  it hidden, or a branch's blocks answered although it predicts the branch not
  taken. There were 0 violations in 14,797 display-logic checks (3 studies) and
  0 in 25,784 branch checks (4 studies). The checks are one-sided: a blank
  answer cannot tell a hidden question from a skipped one, so 2,846 branch
  checks where the evaluator predicts the branch taken but nothing was answered
  count as unknown, not as agreement, and 1,001 branch checks could not be
  evaluated (a field the export lacks, or an undecidable comparison). All 25,784 branch checks (and 1,200 display checks) depend on
  final embedded values rather than the values at the moment of the decision:
  this is not a replay of each session, and it cannot show that a respondent
  saw what the runtime would have shown.

Simulated rows leave timing and meta-info columns blank. Because export tags can
repeat, `run.cells(survey)` gives the exact column layout; `run.row()` is a
dict keyed by name.

## Command line

```bash
surveyir inspect study.qsf               # summary + any load diagnostics
surveyir inspect *.qsf --extras          # which source keys are not yet typed
surveyir convert study.qsf -t markdown -o study.md
surveyir convert study.qsf -t json --opt compact=true
surveyir convert study.qsf -t codebook -o codebook.csv
surveyir design study.qsf                # the experimental design
surveyir simulate study.qsf -n 500 -o sim.csv   # random answers, Qualtrics-style CSV
surveyir formats                         # installed loaders and exporters
surveyir schema -o surveyir.schema.json  # JSON Schema for the IR
```

## The IR at a glance

| Node | What it holds |
|---|---|
| `Survey` | metadata, `blocks`, `questions`, `flow`, scoring, options, `diagnostics` |
| `Block` | ordered `QuestionRef`s and `PageBreak`s, question randomization, `LoopAndMerge` |
| `Question` | a union on `kind`: `choice`, `matrix`, `text_entry`, `slider`, `constant_sum`, `rank_order`, `side_by_side`, `pick_group_rank`, `drill_down`, `highlight`, `hot_spot`, `heat_map`, `graphic_slider`, `file_upload`, `signature`, `descriptive`, `timing`, `meta_info`, `captcha`, `unsupported` |
| `Choice` / `ScalePoint` | id, `Text`, recode, variable name, text entry, exclusive, display logic |
| `FlowNode` | `block`, `embedded_data`, `branch`, `randomizer`, `group`, `end_survey`, `web_service`, `library_block`, `quota`, … |
| `Condition` | `And` / `Or` / `Comparison(left: Operand, operator, right)` |
| `Text` | `html`, `plain`, and parsed `pipes` |

See [docs/design.md](docs/design.md) for the design rationale and
[docs/parity.md](docs/parity.md) for exactly which Qualtrics features are
covered and how they are tested.

## Writing an exporter

```python
from surveyir import Exporter

class SurveyJSExporter(Exporter):
    name = "surveyjs"
    extension = ".json"
    summary = "SurveyJS form definition"

    def __init__(self, *, theme: str = "default"):
        self.theme = theme

    def export(self, survey):
        ...
```

Register it from your own package so `surveyir convert -t surveyjs` finds it:

```toml
[project.entry-points."surveyir.exporters"]
surveyjs = "my_package.exporters:SurveyJSExporter"
```

Loaders for other platforms (LimeSurvey, REDCap, SurveyMonkey, …) plug in the
same way under `surveyir.loaders`.

## Status

Alpha. The IR schema is versioned (`Survey.schema_version`) and may still
change before 1.0. Nine newer Qualtrics question types (video response, tree
testing, location selector, and others) have no public QSF example yet. They
load as `unsupported` with their payload intact and a warning that names the
probable type. If you have one, please contribute it: a single exported
example is enough to model a type.

## Roadmap

- Typed handling for quotas that branch or end the survey based on live counts across
  many simulated respondents (counted today, but only `EndCurrentSurvey` is acted on).
- Loaders for the Qualtrics v3 survey-definitions API, LimeSurvey and REDCap.
- Typed conjoint definitions, and a typed model for each question type that
  still lacks a public example. Video response is next; the plan is in
  [docs/design.md](docs/design.md#deferred-video-response-questions).
- A QSF writer, so round-tripping becomes a parity check.

## License

Apache-2.0
