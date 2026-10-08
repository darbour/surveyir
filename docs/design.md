# Design

## Goals

1. **One representation, many uses.** Loaders normalize platform formats into
   the IR; exporters and tools only ever see the IR.
2. **Lossless by construction.** Loaders read source payloads through a
   read-tracking view (`Payload`). Every key that is not read into a typed
   field ends up in that node's `extras`, verbatim. `surveyir inspect --extras`
   and `docs/parity.md` show exactly which keys are not yet typed.
3. **Never silently wrong.** Anything the loader cannot represent becomes an
   `Unsupported*` node plus a `Diagnostic` on `Survey.diagnostics`.
   `strict=True` turns any warning into a `LoadError`. At run time, every
   approximation is recorded in the run's audit. Only strict execution (the
   default) also *prevents* a wrong run: it stops at the first approximation
   that could change assignment, exposure, routing or outcome. With
   `strict=False` or `allow=`, a run can still differ from what Qualtrics would
   show; it is recorded, not prevented.
4. **Executable where the file says enough.** Logic, randomization, Loop &
   Merge and piped text are modeled precisely enough for a run-time walker to
   administer the instrument. Where the file does not say enough (JavaScript,
   web services, fields supplied from outside), strict execution refuses to
   guess.
5. **Language-neutral.** The IR is JSON with a published JSON Schema
   (`schema/surveyir.schema.json`).

## What surveyir claims

Three claims, kept separate because they need different evidence:

| Claim | Meaning | Evidence |
|---|---|---|
| **Information preserved** | The IR holds everything in the source file. | Source accounting: every key is read into a typed field or kept verbatim in `extras` (`surveyir inspect --extras`, [parity.md](parity.md)). IR → JSON → IR round trips are exact on all 880 corpus files. There is no QSF writer yet, so .qsf → IR → .qsf is not checked. |
| **Instrument administered faithfully** | A simulated respondent is shown what the instrument specifies, in that order, under the recorded assignment, and is given only what was shown. | Exact traces written by hand from small instruments and checked in CI (`tests/test_exact_traces.py`): what is displayed, what is not, and the history available at each response. Strict execution, which stops at anything it cannot execute exactly. Randomizer frequencies compared with real exports, and one-sided consistency checks against observed responses (below). |
| **Responses behaviorally valid** | Simulated answers resemble what people would answer. | Out of scope for the core. It depends on the respondent model and needs separate empirical validation against human data, study by study. surveyir supplies the trace and audit such a validation needs. |

The second claim holds only for strict runs. A permissive run (`strict=False`,
or a gap accepted with `allow=`) records each approximation, with what it could
affect, in `run.audit`, but is not a faithful administration where those
approximations apply.

## Decisions

**Pydantic v2 models, discriminated unions.** Question kinds and flow nodes are
unions on `kind` / `type`. Consumers get exhaustive type checking in Python and
a precise `oneOf` in the JSON Schema. `extra="forbid"` catches typos when IR is
built by hand.

**Question kind is semantic, origin is kept.** `kind` says what the question
does (`choice`, `matrix`, …). `mode`/`layout` say how it is presented, and
`origin` keeps the platform's own `(type, selector, sub_selector)` triple.
Twenty-two distinct Qualtrics triples appear in the test corpus, so a flat
"type" field would lose information.

**Ids are always strings, order is explicit.** Qualtrics mixes integer and
string choice ids (`ChoiceOrder: [1, "2"]`). The IR normalizes ids to strings,
and every list (`choices`, `rows`, `columns`, `items`, `Block.elements`) is in
default presentation order. Randomization is described separately, never
pre-applied.

**One condition grammar.** Branch, display, choice-display, skip and
custom-validation logic share Qualtrics' `BooleanExpression` structure. The
loader parses them once into `And`/`Or`/`Comparison`. Qualtrics evaluates AND
before OR, both within a logic set and across sets
([Qualtrics docs](https://www.qualtrics.com/support/survey-platform/survey-module/using-logic/)).
The tree encodes that precedence, so consumers evaluate it as written.
Operators are normalized (`EqualTo` → `equal`), and `source_operator` keeps the
original.

**Piped text is parsed, not resolved.** `Text.pipes` lists every `${...}`
reference with its kind, question id, loop iteration and selector.
`Text.render(resolver)` substitutes values. The IR never guesses a value.

**Blocks keep page breaks.** `Block.elements` is the ordered list of
`QuestionRef` and `PageBreak`, and `Block.pages()` groups them. Skip logic
lives on the `QuestionRef`, as it does in Qualtrics.

**Built from evidence.** Every mapping was written against real payloads: 19
committed fixtures plus 861 public QSFs from GitHub (`corpus/manifest.json`).
Column rules were checked against the header rows of 2,076 public Qualtrics
exports. Several obvious guesses turned out wrong. Question randomization uses
`SubSet`, not `Subset`. Block per-page settings live under
`Randomization.Advanced`. Slider columns are named by item id in UI exports
but by position in some API exports. That is why the corpus tests matter more
than the unit tests.

**Export settings are options, not guesses.** Split multi-value fields,
display-order layout, slider naming, metadata and subsets are chosen when data
is exported and are not in the .qsf; `ColumnOptions` takes them (UI or v3 API
names), and `infer_options(read_header(...))` recovers them from a real export.
Slider naming in particular cannot be predicted: two sliders with identical
.qsf settings (no `ChoiceDataExportTags`) were exported by id in one study and
by position in another, so it depends on the export path.

**Data columns are derived, not stored.** `surveyir.columns` computes export
column names and ImportIds from the IR, following Qualtrics' rules:
- single choice → `tag`
- multiple choice → `tag_<choiceId>`, with carried-forward choices as `tag_x<id>`
- matrix → `tag_<rowPosition>`, or the row's custom export tag
- side-by-side → `tag#<n>_<rowPosition>`
- pick-group-rank → `tag_<g>_GROUP_<id>` and `tag_<g>_<id>_RANK`
- file upload → `tag_Id`, `_Name`, `_Size`, `_Type`
- text entry → `tag` (ImportId `QIDn_TEXT`)
- Loop & Merge → `<loop>_` prefix
- randomized choices → `tag_DO_*` display-order columns

These rules are verified against the header rows of real exports. Of 2,076
public export headers, 1,875 pair with a .qsf from the same repository; in
those, 36,102 of 36,155 question columns (99.85%) match exactly, and 67,641 of
67,697 (99.92%) when metadata, score and display-order columns are included
(`scripts/parity_report.py`, [parity.md](parity.md)). Export settings that change the layout (split
multi-value fields, single-column display order, slider naming) are
`ColumnOptions`.

**Plugins via entry points.** Loaders (`surveyir.loaders`) and exporters
(`surveyir.exporters`) are discovered with `importlib.metadata`, so third-party
formats need no changes here.

## Runtime: design and simulation

`surveyir.runtime` is computed from a `Survey` and never stored in it, so the IR
stays a format, not a session.

**Sample, never enumerate.** The original Twin-2K parser enumerated every path,
which cannot scale: six 1-of-3 randomizers in a random order already give
3^6 × 6! paths. `design()` describes randomization points and their nesting.
`Simulator` samples one respondent at a time.

**Semantics come from data.** Measured on 13,879 real respondents before coding:
- "Present k of n" shows exactly k children, in a uniformly random order.
- "Evenly present" balances which children are shown (least-filled first), not
  their order.
- Display-order cells hold 1-based positions, blank when not shown.
- When every child of a show-all randomizer sets the same field, the last one
  shown wins. `design()` reports such a factor as an order contrast with a
  recorded field. Whether respondents are then exposed to one stimulus or to all
  of them depends on what the arms display and on later branches that read the
  field, so `exposures(run, survey)` derives each respondent's exposure history
  from the trace instead.

Simulated "evenly present" balance is tighter than in real Qualtrics data (arm counts
differ by at most 1, versus 1–6 observed). That gap probably comes from concurrent
respondents and drop-outs in the real data.

**Determinism.** Each respondent has a seed. Counterbalancing state is shared
across the simulated sample and serializable, so the same seed and the same
state reproduce a run exactly.

**Not reproducible from a .qsf:** randomization done in JavaScript or by web
services. `design().opaque` lists those. At run time, a displayed question whose
JavaScript could change assignment or exposure (anything beyond comments and
empty handlers) is recorded as a structured `Approximation` with code
`"javascript"`, and an unimplemented web service as `"web_service"`. Strict
execution stops the run there with `ExecutionError`, unless the caller supplies
an implementation (`implementations=`) or explicitly accepts the gap
(`allow=`, recorded as `Allowed`).

**Consistency checks, not validation.** `scripts/validate_runtime.py` feeds
each finished real respondent's recorded embedded data and answers to the logic
evaluator (`tests/fixtures/validation/twin.json`, 13,816 finished respondents
in 19 studies). A violation is an answer the evaluator predicts hidden, or a
branch's blocks answered although it predicts the branch not taken: 0 of 14,797
display-logic checks (3 studies) and 0 of 25,784 branch checks (4 studies). The
checks are one-sided. A blank cannot tell a hidden question from a skipped one,
so the 2,846 branch checks predicted taken but unanswered are unknown, not
agreement; 1,001 branch checks could not be evaluated (a field the export
lacks, or a comparison the evaluator cannot decide). All 25,784 branch
checks, and 1,200 of the display checks, read embedded fields set inside the
flow, and the state is rebuilt from *final* values rather than replayed, so a
field changed after the decision would go unnoticed. The checks can reveal a
contradiction; they cannot show that a respondent saw what the runtime would
show. The exact-trace benchmark covers that, on synthetic instruments.

**Replay against real responses.** The same script also re-administers each
finished respondent through the simulator (`runtime.replay`): the recorded
flow-randomizer, block and choice orders (`_DO` columns) are forced through the
chooser seam, the recorded answers are given as each question is displayed, and
panel fields are supplied as inputs, so every branch and display condition is
evaluated with the state at that point rather than the final state. Of 13,816
finished respondents, 12,815 were replayed. No answered question went
undisplayed in 219,853 answered questions (0 violations), and in randomized
blocks with a recorded display order the replay showed exactly the questions
Qualtrics recorded as shown; 1,973 questions were
displayed but left blank (unknown: mostly optional comment boxes, and choices
present in the QSF but missing from the export). The fields the flow sets
ended as exported in all 16,246 comparisons. (Replay first found 747
disagreements, all in `story_beliefs`: the runtime stored embedded values as
plain text while Qualtrics keeps them as typed, HTML and whitespace included.
Embedded data is now stored as typed.) (`promiscuous_donors`' 32,759 profile fields
copy fields its question JavaScript sets, which the export lacks, so they are
not compared.) Not replayed: the 1,001
`obedient_twins` respondents, whose flow branches on a field that is neither
declared nor exported and whose answers contradict the QSF's flow. Where a
study's export has no display-order column (one block, and the loops of three
studies), the
questions or iterations shown are inferred from the answers, so those orders
cannot produce a violation themselves.

## What is deliberately out of scope (for now)

- **Executing JavaScript** (`Question.javascript`) and web services. They are
  preserved, not run; strict execution stops where they could matter, and
  `implementations=` lets a caller supply their effect.
- **Behavioral validity** of simulated responses. That needs empirical
  validation against human data for the respondent model and study at hand.

## Deferred: video response questions

Qualtrics' Video Response question is recognized but not modeled. Its code is known
from Qualtrics' survey-taking runtime (`FileUpload` / `VideoCapture` /
`VideoToText`), but no public .qsf, API schema or open-source tool shows its
payload keys (recording length, camera and audio options, transcription
settings). It loads as `unsupported`, payload verbatim, with a warning that names
it.

Planned when picked up:

1. **Minimal typed model, no example needed.** A `video_response` kind with only
   the fields that are certain: text, display logic, validation. Settings stay in
   `extras`. For simulation, the answer is a transcript string, since Qualtrics
   transcribes video responses automatically. Export columns would follow the
   file-upload rule (`_Id`, `_Name`, `_Size`, `_Type`) and be marked
   `evidence="inferred"`.
2. **Full model from one real export.** A single .qsf containing one video
   question, from an account with the Video Response license, would show the
   actual keys. The same applies to Tree Testing, Unmoderated User Testing, ArcGIS
   Map, Location Selector, Solicit Reviews and Org Hierarchy (see
   docs/parity.md).

## Versioning

`Survey.schema_version` follows semver at the minor level: additive changes
bump the minor version, breaking changes bump the major version. The loader that
produced a document is recorded in `Survey.source.loader`.
