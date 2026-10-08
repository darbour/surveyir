# Positioning

Tools that put survey instruments in front of simulated respondents do three
different things, and it helps to keep them apart:

1. **Importing an instrument.** Reading a Qualtrics `.qsf` (or another format)
   into something a program can use.
2. **Representing questionnaire structure.** A model of questions, blocks, flow,
   branches, randomizers and loops that a researcher can inspect.
3. **Executing the instrument.** Administering it to a simulated respondent so
   that routing, randomization and display logic are enforced, and the
   respondent sees only what a real respondent would have seen at that point.

A tool can do the first two well without doing the third. This page compares
surveyir with three related projects on these three points. Statements about
other projects' code are as of 2026-10-08 and may change.

| | Imports QSF | Represents structure | Executes routing in code |
|---|---|---|---|
| ExploraTwin / surveytwin-oss | Yes | Converts to an LLM-readable survey | Partly: static surveys go to the model in one call; dependent questions are run in stages |
| EDSL (Expected Parrot) | Parser in the core repo; `Survey.from_qsf` commented out | Questions and blocks | The commented-out converter does not carry over branching or flow randomizers |
| DDI-Lifecycle | Not applicable (a metadata standard) | Yes: instruments, sequences, `IfThenElse`, `Loop` | No: conditions are stored as code text |
| surveyir | Yes | Typed IR | Yes, in a walker; strict about what it cannot execute |

## ExploraTwin and surveytwin-oss

ExploraTwin is a non-profit platform for digital-twin survey simulation
(Venkat, Qiu, Peng, Gui and Toubia,
[arXiv:2608.20539v2](https://arxiv.org/html/2608.20539v2)), with open-source
code at [nav-v/surveytwin-oss](https://github.com/nav-v/surveytwin-oss).
According to the paper:

- In static surveys, "each twin completes the full survey in one model call".
  If later questions depend on earlier answers, the run is staged.
- The authors note that staging "may re-ask a question after the model has seen
  later parts of the survey, creating a different information state from the
  original survey flow."
- A small share of twin responses "can still violate the survey's structure
  because the LLM completes the instrument in context rather than through the
  rule-enforcing interface used by human respondents."
- Features the pipeline cannot fully reproduce, such as custom Qualtrics
  JavaScript or externally defined embedded data, are flagged with warnings
  before the run rather than executed.

The paper states these limits openly. In this approach the model sees the
instrument, and routing is largely left to the model. surveyir's intended
contract is the reverse: the code does the routing, and the model sees only
what has been displayed so far.

## EDSL (Expected Parrot)

[EDSL](https://github.com/expectedparrot/edsl) contains a QSF parser,
[`edsl/surveys/qsf_parser.py`](https://github.com/expectedparrot/edsl/blob/main/edsl/surveys/qsf_parser.py),
that reads questions, blocks and the flow tree. As of commit `388a479`
(2026-10-07):

- `Survey.from_qsf` in
  [`edsl/surveys/survey.py`](https://github.com/expectedparrot/edsl/blob/main/edsl/surveys/survey.py)
  is commented out.
- [`edsl/surveys/TODO.md`](https://github.com/expectedparrot/edsl/blob/main/edsl/surveys/TODO.md)
  says "QSF import/export now lives in a separate plugin". We have not reviewed
  that plugin.

The commented-out converter mapped questions to EDSL question types and blocks
to question groups. It did not translate branch logic: its docstring says
advanced branching "may require manual adjustment". It listed the questions in
blocks under a flow randomizer as `questions_to_randomize`. In EDSL that option
randomizes the order of a question's answer options. It does not assign the
respondent to one arm. So, as far as we can tell from the code, the old path
neither executed branching nor executed flow randomizers.

## DDI-Lifecycle

[DDI-Lifecycle 3.3](https://docs.ddialliance.org/DDI-Lifecycle/3.3/model/) is a
metadata standard for describing studies, including their data-collection
instruments. It models instruments as control constructs such as sequences,
[`IfThenElse`](https://docs.ddialliance.org/DDI-Lifecycle/3.3/model/item-types/IfThenElse/)
and [`Loop`](https://docs.ddialliance.org/DDI-Lifecycle/3.3/model/item-types/Loop/):

- An `IfThenElse` condition is "expressed as a CommandCode. The condition is an
  expression in the programming language used in the instrument."
- A loop's initial value, step and `LoopWhile` condition are also
  [CommandCode](https://docs.ddialliance.org/DDI-Lifecycle/3.3/model/composite-types/CommandCodeType/).
  This is an in-line command, a link to an external script, or an extension stub.

The standard describes where logic sits and what it is for. We found no
semantics in it for evaluating that code, so a DDI instrument, as described,
records routing rather than executing it. That is a reasonable choice for an
archival standard, and it is complementary to surveyir: a typed IR with
executable conditions could, in principle, be exported to DDI's descriptive form.

## What surveyir aims to add

surveyir also imports QSF files and represents their structure. Its intended
differentiator is narrower, and it is **a claim still to be demonstrated**:

> an independently usable, inspectable execution layer that enforces routing in
> code and makes the respondent's information state auditable.

In practice this means three things:

- Branches, randomizers (including even presentation), display and skip logic,
  loops and piped text are executed by the walker, not left to a model.
- Each run records what was displayed, in what order, and under which assignment.
- What the walker cannot execute (JavaScript, web services, unevaluable
  conditions) is reported, not silently approximated.

What has been shown so far is limited:

- Simulated randomizer frequencies match real Qualtrics exports.
- Logic predictions are consistent with observed responses in 19 Twin-2K-500
  studies (see `scripts/validate_runtime.py`). These are consistency checks
  against final recorded data, not a replay of each respondent's session.

What remains to show is the end-to-end study. It would take an instrument with
a text-vignette treatment and a human benchmark, run it through the execution
layer with a respondent model, and run the original analysis. The study should
report execution failures (from traces and the audit) separately from
predictive discrepancies between simulated and human estimates. It should also
compare against a simpler baseline that sends questions in isolation.
