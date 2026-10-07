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
   `strict=True` turns any warning into a `LoadError`.
4. **Simulation-ready.** Logic, randomization, Loop & Merge and piped text are
   modeled precisely enough that a run-time walker can reproduce what a
   respondent would see.
5. **Language-neutral.** The IR is JSON with a published JSON Schema
   (`schema/surveyir.schema.json`).

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

These rules are verified against the header rows of real exports: 99.85% of
36,155 columns match exactly. Export settings that change the layout (split
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
  shown wins. `design()` reports that as a 1-of-n assignment.

Simulated "evenly present" balance is tighter than in real Qualtrics data (arm counts
differ by at most 1, versus 1–6 observed). That gap probably comes from concurrent
respondents and drop-outs in the real data.

**Determinism.** Each respondent has a seed. Counterbalancing state is shared
across the simulated sample and serializable, so the same seed and the same
state reproduce a run exactly.

**Not reproducible from a .qsf:** randomization done in JavaScript or by web
services. `design().opaque` lists those, and runs record a note when one is
reached. `Simulator(web_service=...)` can emulate a web service.

## What is deliberately out of scope (for now)

- **Executing JavaScript** (`Question.javascript`) and web services. They are
  preserved, not run.

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
