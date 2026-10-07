# Simulating respondents

`Simulator` runs respondents through a survey the way Qualtrics would. It
handles randomizers (including "evenly present" counterbalancing), branches,
display and skip logic, question, choice and loop order, Loop & Merge, carry
forward and piped text. Your *answerer* is called for each question the
respondent is shown; the simulator decides what comes next.

How this was checked against real respondents is described in
`tests/test_runtime_validation.py`, and summarized in the README.

## One respondent

```python
import surveyir
from surveyir.runtime import RandomAnswerer, ScreenerAwareAnswerer, Simulator

survey = surveyir.load_qsf("tests/fixtures/qualtrics/obedient_twins.qsf")
sim = Simulator(survey, seed=1)
run = sim.respondent(RandomAnswerer(seed=2))
print("shown:", [qid for qid, _ in run.displayed])
print("ended by:", run.ended_by)
```

```text
shown: ['QID34', 'QID31']
ended by: FL_63
```

This respondent stopped early. The survey screens out anyone whose answer to
`Q57` (QID31) doesn't contain "use", and random text fails that test. Without
answers that satisfy the survey's own logic, every simulated respondent would
leave at the screener.

`ScreenerAwareAnswerer` answers randomly, except its free-text answers contain
whatever the survey's logic looks for:

```python
run = sim.respondent(ScreenerAwareAnswerer(survey, seed=2))
print(len(run.displayed), "questions shown; ended by:", run.ended_by)
print("conditions:", {k: v for k, v in run.embedded.items() if k.startswith(("absurd", "s_"))})
print("randomizers:", dict(list(run.flow_order.items())[:3]))
```

```text
24 questions shown; ended by: FL_68
conditions: {'absurd_1': 'B', 'absurd_2': 'A', 'absurd_3': 'B', 's_1': 'test', 's_2': 'control', 's_3': 'control'}
randomizers: {'FL_6': ['FL_8'], 'FL_15': ['FL_16'], 'FL_12': ['FL_14']}
```

This respondent went through all of it and left at `FL_68`, the End of Survey
element at the end of the flow (the screener's exit is `FL_63`).

A `RespondentRun` records everything:

| attribute | contents |
|---|---|
| `displayed` | `(question id, loop id)` for each question shown, in order |
| `answers` | `{(question id, loop id): Answer}` |
| `embedded` | embedded data at the end of the survey |
| `flow_order` | `{randomizer id: [arm keys in the order shown]}` |
| `block_order` | `{(block id, loop id): [question ids shown, in order]}` |
| `choice_order` | `{(question id, loop id): [choice ids in the order shown]}` |
| `finished`, `ended_by`, `response_flag` | how the respondent left |
| `notes` | anything the simulator couldn't reproduce (web services, JavaScript) |

## Writing an answerer

An answerer is any callable `answer(view, state) -> value`. The `view` is the
question as the respondent sees it:

| `QuestionView` field | contents |
|---|---|
| `question` | the IR question (`view.kind` is its kind) |
| `text` | question text with piped text filled in |
| `choices` | `ChoiceView(id, text)` in the order shown: choices, statements, items or fields |
| `columns` | matrix scale points, in the order shown |
| `loop` | the Loop & Merge iteration, if any |
| `block_id`, `page`, `scale_flipped` | where it is shown and whether the scale was reversed |

Return `None` to skip, or a value shaped for the question kind:

| kind | value |
|---|---|
| `choice` | a choice id; a list of ids if `multiple` |
| `matrix` | `{row id: column id}` (`multiple`: lists; `text`: `{row: {column: text}}`) |
| `text_entry` | a string (`form`: `{field id: text}`) |
| `slider`, `constant_sum` | `{item id: number}` |
| `rank_order` | `{item id: rank}` |

Wrap the value in `Answer(value, text={choice id: "..."})` to fill in an
"Other, please specify" box.

Here is a rule-based respondent who always agrees with whatever argument they're
shown and picks the first choice otherwise:

```python
from surveyir.model import ChoiceQuestion, SliderQuestion, TextEntryQuestion

def agreeable(view, state):
    q = view.question
    if isinstance(q, SliderQuestion):
        return {c.id: 50 for c in view.choices}   # exactly in the middle
    if isinstance(q, TextEntryQuestion):
        return "I will not use AI to answer this survey."
    if isinstance(q, ChoiceQuestion) and view.choices:
        return view.choices[0].id
    return None

run = Simulator(survey, seed=4).respondent(agreeable)
print(run.embedded.get("side1"), "|", run.flow_order.get("FL_42"))
```

```text
a good idea | ['FL_29']
```

With `attitude` at exactly 50, this respondent reached the FL_42 randomizer,
which only runs for undecided people.

## Many respondents and counterbalancing

`sim.run(n, answerer)` returns `n` runs. Randomizers marked "evenly present" are
balanced across the whole simulated sample, least-filled first, as Qualtrics
does. Others are independent coin flips:

```python
from collections import Counter

sim = Simulator(survey, seed=7)
runs = sim.run(300, ScreenerAwareAnswerer(survey, seed=8))
print(Counter(r.embedded["s_1"] for r in runs))
print(Counter(r.flow_order["FL_6"][0] for r in runs))
```

```text
Counter({'control': 150, 'test': 150})
Counter({'FL_8': 150, 'FL_7': 150})
```

The balancing state lives in `sim.balancer`. To continue a simulation later
with the same balance, save it and pass it back:

```python
state = sim.balancer.to_dict()
from surveyir.runtime import Counterbalancer
later = Simulator(survey, seed=9, balancer=Counterbalancer.from_dict(state))
print(later.balancer.counts["FL_50"])
```

```text
{'FL_51': 150, 'FL_52': 150}
```

A run is reproducible: the same seed and the same balancer state give the same
assignments, orders and (with a seeded answerer) answers.

## Rows like a Qualtrics export

`run.cells(survey)` gives `(Column, value)` pairs in export order, using the same
columns as `surveyir.response_columns(survey)`. Values follow Qualtrics' numeric
export: recode values (or choice ids) for choices, `1`/blank for split
multi-select columns, display positions in display-order columns, and blank for
questions the respondent never saw.

```python
run = runs[0]
for column, value in run.cells(survey):
    if column.name.startswith(("attitude", "s_1", "FL_50_DO", "Imagine1_DO")):
        print(f"{column.name:<18} {value!r}")
```

```text
attitude_1         70
Imagine1_DO_Q8     ''
Imagine1_DO_Q9     1
Imagine1_DO_i1_1   2
Imagine1_DO_i1_2   3
FL_50_DO_FL_51     1
FL_50_DO_FL_52     ''
s_1                'control'
```

`run.row(survey)` returns the same as a `{name: value}` dict. Two questions can
share an export tag, so prefer `cells` when writing files. To write a CSV:

```python
import csv, io

buf = io.StringIO()
writer = csv.writer(buf)
writer.writerow([c.name for c in surveyir.response_columns(survey)])
for run in runs[:3]:
    writer.writerow([value for _, value in run.cells(survey)])
print(buf.getvalue().splitlines()[0][:80])
```

```text
consent non_twins,consent twins,Q57,Q4_First Click,Q4_Last Click,Q4_Page Submit,
```

Simulated rows leave timing and meta-info columns blank. Pass
`ColumnOptions(include_metadata=True)` to add `StartDate`, `ResponseId` and the
other metadata columns (see [Data columns](data-columns.md)).

## Respondent attributes

Fields Qualtrics would receive from a panel or URL (`Recipient` embedded data such
as `PROLIFIC_PID`), the device type and location are inputs to a run:

```python
sim = Simulator(survey, seed=1, device="mobile", location={"City": "Columbus"})
run = sim.respondent(ScreenerAwareAnswerer(survey, seed=1),
                     embedded={"PROLIFIC_PID": "abc123"})
print(run.embedded["PROLIFIC_PID"], run.state.device)
```

```text
abc123 mobile
```

## Web services and JavaScript

The simulator can't call web services or run question JavaScript. When a
respondent reaches either, the run gets a note, and fields a web service would set
are left empty. To emulate a web service, pass a function that returns the
fields it sets:

```python
import random

rng = random.Random(0)
def fake_rng_service(node, state):
    return {name: str(rng.randint(1, 4)) for name in node.sets_fields}

sim = Simulator(survey, web_service=fake_rng_service)
```

## Evaluating logic directly

The logic evaluator works on its own, given a `RespondentState`:

```python
from surveyir.model import BranchNode
from surveyir.runtime import Answer, RespondentState, evaluate

screener = next(n for n in survey.walk_flow() if n.id == "FL_62")
print(surveyir.describe_condition(screener.condition, survey))
state = RespondentState()
state.answers[("QID31", None)] = Answer("I will not use AI")
print(evaluate(screener.condition, state, survey))
state.answers[("QID31", None)] = Answer("ok")
print(evaluate(screener.condition, state, survey))
```

```text
Q57 text does not contain use
False
True
```

Comparisons are numeric when both sides parse as numbers, and textual otherwise
(case-sensitive unless the condition says to ignore case). AND binds tighter than
OR, as in Qualtrics. Anything the state can't answer, such as a GeoIP condition,
evaluates false and leaves a note in `state.notes`.

## Respondents powered by an LLM

`examples/llm_respondents.py` runs personas through a survey with Claude. Each
question goes to the model as the respondent sees it: `view.text` with piped
text filled in, and `view.choices` in the order shown. The response format is
constrained to a JSON schema whose `enum` is the displayed choice ids, so every
answer is valid:

<!-- no-run -->
```python
import anthropic
from examples.llm_respondents import claude_answerer

client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
answerer = claude_answerer(client, "You are a 61-year-old retired engineer in Arizona.")
run = Simulator(survey, seed=1).respondent(answerer)
```

The example covers choice, single-answer matrix and text questions. It returns
`None` (no answer) for other kinds, and for any request that is refused. It
enables Anthropic's server-side refusal fallbacks: if a request is declined, the
API retries it on a fallback model inside the same call. Remove `betas` and
`fallbacks` from the request to turn that off.

Run it with `uv run --with anthropic python examples/llm_respondents.py`.
