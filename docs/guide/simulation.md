# Simulating respondents

`Simulator` runs respondents through a survey the way Qualtrics would. It
handles randomizers (including "evenly present" counterbalancing), branches,
display and skip logic, question, choice and loop order, Loop & Merge, carry
forward, quotas and piped text. Your *respondent* is asked to answer each
question as it is shown; the simulator decides what comes next.

Execution is strict by default: a run stops with `ExecutionError` at the first
thing the simulator can't administer exactly (question JavaScript, a web
service, a panel field nobody supplied, ...). `surveyir check` lists these gaps
before you run anything; see [Strict execution](#strict-execution).

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
| `trace` | what the respondent perceived, in order (see [below](#the-trace-and-the-audit)) |
| `audit` | what the runtime decided and every approximation it made |
| `privileged` | True if the answers came from a legacy answerer that could read hidden state |
| `notes` | the approximations, as text |

## Writing a respondent

A respondent is any object with an `answer(ctx)` method. `ctx` is a
`ResponseContext` holding only what the respondent has perceived:

| `ResponseContext` field | contents |
|---|---|
| `view` | the question to answer, as a `Display` |
| `page` | every `Display` visible on the current page (a convenience: these are already in `history`) |
| `history` | the trace so far, current page included: pages, every screen shown (text-only ones included) and the respondent's own answers |
| `respondent_index` | which respondent this is |

A `Display` has the rendered `text` (piped values filled in), `choices` and
`columns` as `ChoiceShown(id, text)` in the order shown, the question `kind`
and `mode`, `multiple`, `required`, and the `validation` the respondent is told
about (`min_chars`, `number_min`, `max_choices`, ...). It holds no IR question,
logic, JavaScript, block or flow ids, and no embedded data that wasn't piped
into the text. `transcript(ctx.history)` renders the history as plain text.

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
shown and picks the first choice otherwise. Its text answers respect the
displayed minimum length:

```python
class Agreeable:
    def answer(self, ctx):
        view = ctx.view
        if view.kind == "slider":
            return {c.id: 50 for c in view.choices}   # exactly in the middle
        if view.kind == "text_entry":
            text = "I will not use AI to answer this survey."
            return (text + " I agree.") * (1 + view.validation.get("min_chars", 0) // 40)
        if view.kind == "choice" and view.choices:
            return view.choices[0].id
        return None

run = Simulator(survey, seed=4).respondent(Agreeable())
print(run.embedded.get("side1"), "|", run.flow_order.get("FL_42"))
```

```text
a good idea | ['FL_29']
```

With `attitude` at exactly 50, this respondent reached the FL_42 randomizer,
which only runs for undecided people.

An answer that breaks a question's validation (too short, not a number, too
many selections) is one Qualtrics would not have accepted. The run records it
as an `answer.invalid` approximation, which stops a strict run. The built-in
`RandomAnswerer` and `ScreenerAwareAnswerer` respect the displayed validation.

A plain function `answer(view, state)` also works. It is given a `QuestionView`
(with the IR question) and the full `RespondentState`, including hidden embedded
data, so its runs are marked `privileged`.

## The trace and the audit

`run.trace` is what the respondent perceived, in order: `PageStart`, `Display`
(every screen, text-only ones included), `Response`, `PageSubmit` and `End`.
`run.audit` is what the runtime knew and the respondent did not: randomizer
decisions with their probabilities, branch results, hidden questions and
choices, embedded data as it was set, skips, and every approximation.

```python
from collections import Counter
from surveyir.runtime import transcript

run = sim.respondent(RandomAnswerer(seed=2))
print([type(o).__name__ for o in run.trace])
print(transcript(run.trace)[-120:])
print(Counter(type(e).__name__ for e in sim.respondent(ScreenerAwareAnswerer(survey)).audit))
```

```text
['PageStart', 'Display', 'Response', 'PageSubmit', 'PageStart', 'Display', 'Response', 'PageSubmit', 'End']
ceed, please type in the below box "I will not use AI to answer this survey"
Answer (QID31): text

--- End of survey ---
Counter({'EmbeddedSet': 9, 'RandomizerDecision': 9, 'Hidden': 6, 'BranchEval': 5})
```

## Many respondents and counterbalancing

`sim.run(n, answerer)` returns `n` runs. Randomizers marked "evenly present" are
balanced across the whole simulated sample, least-filled first, as Qualtrics
does. Others are independent coin flips:

```python
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
assignments, orders and (with a seeded answerer) answers. Seed the `Simulator`,
not `run`: `sim.run(n, seed=...)` would give every respondent the same random
stream, so strict runs reject it.

## Choosers and replay

Every randomization decision goes through a *chooser*: which arms of a flow
randomizer are shown, the order of a block's questions, of a question's choices
or matrix columns, of loop iterations, and the respondent's scale flip. The
chooser gets a `ChoiceRequest` (`kind`, `node_id`, the `options`, how many to
present `k`, the `loop_id`, ...) and returns the presented options in order,
optionally with `p_given_history` for the audit. The walker keeps everything
else: fixed positions, display logic, what follows from the assignment. The
default, `DefaultChooser`, draws as Qualtrics does. Pass your own with
`Simulator(..., chooser=...)`, for example to assign arms from an external
system:

```python
from surveyir.runtime import DefaultChooser

class SecondArm(DefaultChooser):
    """Always the second arm of FL_6; everything else drawn as usual."""

    def choose(self, req, rng, balancer):
        if req.kind == "flow" and req.node_id == "FL_6":
            return ("FL_8",), None
        return super().choose(req, rng, balancer)

forced = Simulator(survey, seed=7, chooser=SecondArm())
runs = forced.run(20, ScreenerAwareAnswerer(survey, seed=8))
print(Counter(r.embedded["absurd_1"] for r in runs))
```

```text
Counter({'B': 20})
```

A chooser that does not call `balancer.choose` leaves the counterbalancing
counts as they were.

*Replay* re-administers a recorded respondent: `ReplayChooser` forces recorded
decisions, keyed `(kind, node_id, loop_id)` and listed as presented, and
`ReplayAnswerer` gives each displayed question its recorded answer. Display
logic, branches and embedded data are evaluated as the respondent goes, with
the state at each point. `record(run)` takes what a replay needs from a
simulated run, and replaying it reproduces the run:

```python
from surveyir.runtime import record, replay

rec = record(runs[0])
again = replay(survey, rec.orders, rec.answers, embedded=rec.embedded, seed=rec.seed)
print(again.trace == runs[0].trace, rec.orders[("flow", "FL_6", None)])
```

```text
True ('FL_8',)
```

To replay a real response, build the orders from its `_DO` columns
(`FL_<id>_DO` for flow randomizers), its answers keyed `(qid, loop_id)`, and
its panel or URL fields as `embedded=`. A replay is strict: a decision the
record does not hold raises `ReplayGap` (an `ExecutionError`), unless you pass
`allow={"replay.gap"}` or `strict=False`, in which case the default chooser
draws it and the gap is recorded in the audit. `fallback_kinds={"columns"}`
draws orders the export never records without counting them as gaps.
`scripts/validate_runtime.py` replays the real respondents of the 19 fixture
studies this way (see `tests/test_runtime_validation.py`).

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

A panel field that the flow declares but nobody supplies is read as empty. That
matters only if logic or piped text reads it, so only then does the run record
it (`embedded.unset`).

## Strict execution

Some things in a survey can't be administered from the `.qsf` alone: question
JavaScript, web services, panel fields, a respondent's location, library blocks.
`surveyir check` (or `executability(survey)`) lists everything administration
involves, and what the simulator does about it, without running anyone:

```python
from surveyir.runtime import ExecutionPolicy, executability

donors = surveyir.load_qsf("tests/fixtures/qualtrics/promiscuous_donors.qsf")
report = executability(donors)
print(report.summary())
print([f.resolution() for f in report.blocking(ExecutionPolicy())])
```

```text
[Promiscuous Donors] 2025.3.31 Digital Twins: 1 needs implementation, 1 preserved only, 6 executable

Needs implementation:
  question_javascript at QID481 (assignment) [javascript]: question JavaScript randomizes or sets embedded data

Preserved only:
  conjoint at conjoint 1 (none): diy_js design metadata (high confidence); its randomization runs only as listed above

Executable:
  branch x1: FL_163
  end_survey x2: FL_162, FL_45
  display_logic x3: QID591, QID592, QID593
["implementations={'javascript': {'QID481': ...}} or allow javascript:QID481 or allow javascript"]
```

A strict run (the default) stops at the first such gap that could change
assignment, exposure, routing or outcome. The error says where, and how to get
past it; the partial run is on `error.run`:

```python
from surveyir.runtime import ExecutionError

try:
    Simulator(donors, seed=1).run(5, ScreenerAwareAnswerer(donors, seed=1))
except ExecutionError as e:
    print(e)
    print([type(o).__name__ for o in e.run.trace][-3:])
```

```text
javascript at QID481 (affects assignment): question QID481 has JavaScript, which is not run (it randomizes or sets embedded data). To proceed, pass implementations={'javascript': ...}, allow={'javascript'} or allow={'javascript:QID481'}, or strict=False.
['PageSubmit', 'PageStart', 'Display']
```

There are three ways through a gap:

- **Implement it.** `implementations=` supplies what the file can't:
  `"javascript"` (`{qid: fn(state) -> {field: value}}`, run when the question
  is displayed), `"web_service"` (`fn(node, state) -> {field: value}`, or a
  dict of them by flow id), `"embedded"` (`{field: value}` for panel fields)
  and `"location"` (`{name: value}` for `${loc://...}` and GeoIP logic). An
  implemented gap is no longer an approximation; the fields it sets appear in
  the audit as `EmbeddedSet` with source `javascript:QID481`,
  `web_service:FL_3` or `respondent`.
- **Allow it.** `allow={"javascript"}` accepts a code everywhere;
  `allow={"javascript:QID481"}` only at one place. The run records the
  approximation and an `Allowed` event.
- **Run permissively.** `strict=False` records every approximation and carries
  on, for exploring a survey.

QID481's JavaScript draws the conjoint profiles. Here is a simplified version in
Python (attribute order fixed), from the design surveyir found in the script:

```python
import random
from surveyir.runtime.trace import Allowed, EmbeddedSet

conjoint, draw = donors.conjoints[0], random.Random(0)

def profiles(state):
    fields = {}
    for task in range(1, conjoint.tasks + 1):
        for i, feature in enumerate(conjoint.features, start=1):
            fields[f"F-{task}-{i}"] = feature.name
            fields[f"F-{task}-1-{i}"] = draw.choice(feature.levels)
    return fields

sim = Simulator(donors, seed=1, implementations={"javascript": {"QID481": profiles}})
runs = sim.run(5, ScreenerAwareAnswerer(donors, seed=1))
reached = [r for r in runs if "F-1-1-1" in r.embedded]  # the others declined consent
print(len(reached), "reached QID481:", [r.embedded["F-1-1-1"] for r in reached])
print({e.source for r in reached for e in r.audit if isinstance(e, EmbeddedSet)
       and e.name.startswith("F-")})

allowed = Simulator(donors, seed=1, allow={"javascript:QID481"})
runs = allowed.run(5, ScreenerAwareAnswerer(donors, seed=1))
print({e for r in runs for e in r.audit if isinstance(e, Allowed)})
```

```text
2 reached QID481: ['Republican', 'Democrat']
{'javascript:QID481'}
{Allowed(code='javascript', location='QID481')}
```

The same flags work on the command line: `surveyir simulate` is strict, with
`--allow CODE[:LOCATION]` and `--permissive`, and exits 1 with the error if a
run is blocked; `surveyir check --strict` exits 1 if anything would block. For a
given policy, `report.blocking(policy)` is empty exactly when nothing stops a
strict run, as far as can be known before answers are given (an invalid answer
or a gap behind a branch nobody reaches can't be predicted statically).

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
OR, as in Qualtrics. An unanswered question is unanswered, not unknown:
`not_selected` and `not_displayed` hold for it. A comparison the state can't
answer (a GeoIP condition with no location, a score, an ordering comparison on
text) is false and is recorded in `state.approximations`; in a strict run it
raises `ExecutionError` instead.

## Respondents powered by an LLM

`examples/llm_respondents.py` runs personas through a survey with Claude. Each
respondent is one conversation built from `ctx.history`: every screen shown so
far (consent forms and treatment vignettes included) and the respondent's own
earlier answers, never the assigned condition or the survey's logic. The
response format is constrained to a JSON schema whose `enum` is the displayed
choice ids, and the question's validation is stated in the request:

<!-- no-run -->
```python
import anthropic
from examples.llm_respondents import claude_answerer

client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
answerer = claude_answerer(client, "You are a 61-year-old retired engineer in Arizona.")
run = Simulator(survey, seed=1, allow={"answer.invalid"}).respondent(answerer)
```

The example covers choice, single-answer matrix and text questions. It returns
`None` (no answer) for other kinds, and for any request that is refused. A
model can still break a question's validation; `allow={"answer.invalid"}`
records that instead of stopping the run. It
enables Anthropic's server-side refusal fallbacks: if a request is declined, the
API retries it on a fallback model inside the same call. Remove `betas` and
`fallbacks` from the request to turn that off.

Run it with `uv run --with anthropic python examples/llm_respondents.py`.
