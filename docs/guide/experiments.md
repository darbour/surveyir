# Experiments

`surveyir.design(survey)` describes what a survey randomizes and how, without
running it. The examples use *Obedient Twins*, a persuasion study with seven
randomized manipulations.

```python
import surveyir

survey = surveyir.load_qsf("tests/fixtures/qualtrics/obedient_twins.qsf")
d = surveyir.design(survey)
print(d.summary())
```

```text
FL_42: assignment to side1, side3 (arms display nothing), 1 of 2, evenly presented if attitude value (choice 1) = 50
  - support (nominal share 0.5): side1='a good idea', side 2='supports', side3='in support'
  - against (nominal share 0.5): side1='a bad idea', side2='opposes', side3='against'
FL_6: assignment to absurd_1 (arms display nothing), 1 of 2, evenly presented
  - FL_7 (nominal share 0.5): absurd_1='A'
  - FL_8 (nominal share 0.5): absurd_1='B'
FL_15: assignment to absurd_2 (arms display nothing), 1 of 2, evenly presented
  - FL_16 (nominal share 0.5): absurd_2='A'
  - FL_17 (nominal share 0.5): absurd_2='B'
FL_12: assignment to absurd_3 (arms display nothing), 1 of 2, evenly presented
  - FL_13 (nominal share 0.5): absurd_3='A'
  - FL_14 (nominal share 0.5): absurd_3='B'
FL_50: assignment to s_1 (arms display nothing), 1 of 2, evenly presented
  - FL_51 (nominal share 0.5): s_1='control'
  - FL_52 (nominal share 0.5): s_1='test'
FL_59: assignment to s_2 (arms display nothing), 1 of 2, evenly presented
  - FL_60 (nominal share 0.5): s_2='control'
  - FL_61 (nominal share 0.5): s_2='test'
FL_56: assignment to s_3 (arms display nothing), 1 of 2, evenly presented
  - FL_57 (nominal share 0.5): s_3='control'
  - FL_58 (nominal share 0.5): s_3='test'
block_questions randomized at BL_cMZntkAHYaNDzEy (advanced)
block_questions randomized at BL_bJCKlj1xzXLsWUe (advanced)
block_questions randomized at BL_3RfCDTdyXhIpQJE (advanced)
Nominal shares are k/n, ignoring branches; not exposure probabilities, nor conditional on balancing history.
Assignments only set fields: later branches, display logic or piped text decide what each respondent sees (see exposures(run, survey)).
```

Reading the summary:

- **Factors.** Each line `FL_…` is a flow randomizer, of one of three kinds.
  In Qualtrics studies the first is the most common: arms set a field, and
  blocks shown later depend on it.
  - "assignment to `absurd_1` (arms display nothing)": the arms only set a
    field. Every factor in this study is like that: display logic on `absurd_1`
    and `s_1` later decides which questions a respondent sees, and the `side`
    fields are piped into question text. What a respondent is shown is decided
    after the randomizer (see [below](#a-recorded-field-is-not-an-exposure)).
  - "exposure contrast, 1 of 2": each respondent is shown one arm's own
    content, and not the other's.
  - "order contrast, 4 of 4": every arm is shown to everyone, in random order,
    so arms differ only in their position.
- **Assignments.** An arm lists the embedded-data values it sets, which is usually
  how the condition is recorded in the data (`s_1='control'`).
- **Nominal shares.** "nominal share 0.5" is k/n: the share of respondents
  reaching the randomizer that the design intends for the arm. It is not a
  respondent's exposure probability, which also depends on branches and on what
  they answer. With "evenly presented" randomizers it isn't the chance of the next
  draw either: least-filled presentation can force the next arm from earlier
  counts. Nested arms also show a nominal marginal, the product of the enclosing
  shares, still ignoring branches. Each simulated draw records its actual
  probabilities, given the counts at the time, in the run's audit.
- **Conditions.** `if …` means the randomizer only runs for respondents who meet
  that branch condition. FL_42 is reached only when `attitude` is exactly 50.
- **Within-subject order.** The `block_questions randomized` lines are question
  order randomized inside a block.

## Factors and arms

```python
for f in d.between_subjects[:3]:
    arms = ", ".join(f"{a.key} (share {a.nominal_share})" for a in f.arms)
    print(f"{f.id}: treatment field {f.treatment_fields}, evenly presented: "
          f"{f.even_presentation}, arms: {arms}")
```

```text
FL_42: treatment field ['side1', 'side3'], evenly presented: True, arms: FL_29 (share 0.5), FL_43 (share 0.5)
FL_6: treatment field ['absurd_1'], evenly presented: True, arms: FL_7 (share 0.5), FL_8 (share 0.5)
FL_15: treatment field ['absurd_2'], evenly presented: True, arms: FL_16 (share 0.5), FL_17 (share 0.5)
```

`treatment_fields` are the fields whose value differs between arms. FL_42 lists
`side1` and `side3` but not `side2`, which points to a bug in the original
study: the *support* arm sets `side 2` (with a space) while the *against* arm
sets `side2`. In the real data, `side2` is blank for everyone assigned to
*support*. Reading the design before fielding a survey catches this kind of
mistake.

`Arm.key` matches the `FL_<id>_DO_<key>` display-order columns of a real export.
`Arm.blocks` lists the blocks inside the arm.

## Crossing and cells

Factors that the same respondents reach, and that are assigned independently,
are fully crossed. `crossed()` groups them; `cells()` enumerates the conditions
of the top-level group:

```python
print([[f.id for f in group] for group in d.crossed()])
cells, complete = d.cells()
print(len(cells), "cells; complete:", complete)
print(cells[0])
```

```text
[['FL_42'], ['FL_6', 'FL_15', 'FL_12', 'FL_50', 'FL_59', 'FL_56']]
64 cells; complete: False
{'FL_6': 'FL_7', 'FL_15': 'FL_16', 'FL_12': 'FL_13', 'FL_50': 'FL_51', 'FL_59': 'FL_60', 'FL_56': 'FL_57'}
```

`complete` is `False` because FL_42 runs only under a branch condition, so its
arms aren't crossed with everything else. `cells()` never enumerates nested or
conditional designs and takes a `limit`, because the number of paths grows fast.
Order randomization is not counted in cells.

## Randomization the file cannot describe

Some studies randomize in JavaScript or through a web service that calls an
external random number generator. The .qsf can't say what those do, so
`design()` lists them separately:

```python
ideas = surveyir.design(surveyir.load_qsf("tests/fixtures/qualtrics/idea_generation.qsf"))
for o in ideas.opaque:
    print(o.kind, o.location, "-", o.detail)
```

```text
javascript QID2 - question JavaScript sets embedded data
javascript QID38 - question JavaScript sets embedded data
javascript QID9 - question JavaScript sets embedded data
```

At run time these become structured approximations in the run's audit: code
`"javascript"` when a displayed question has JavaScript that could change
assignment or exposure (anything beyond comments and empty handlers), and
`"web_service"` when an unimplemented web service is reached. Strict execution,
the default, stops the run there with `ExecutionError`. To run anyway, supply
the missing behaviour with `implementations=`, or accept the approximation
explicitly with `allow=` (see [Simulating respondents](simulation.md)).

## A recorded field is not an exposure

*Privacy* has six conditions, and its randomizer FL_4 runs all six arms for
every respondent, in random order. The arms display nothing: each one only sets
the field `Group`, so the value that sticks is the last one run. The real data
confirms this: every respondent's exported `Group` equals the last arm in their
display order. Branches after the randomizer then read `Group` and show one of
six group blocks.

So three things that are easy to conflate differ here:

- the **randomization node**: FL_4, which runs six field assignments in random
  order;
- the **recorded field**: `Group`, which holds the last arm run;
- the **exposure**: the one group block a branch on `Group` shows.

Because the arms display nothing, `design()` reports FL_4 as an **assignment**
recorded in `Group`: a 1-of-6 assignment in effect, whose exposure is decided by
later logic. Had each arm displayed its own block, the same structure would be
an **order** contrast: every respondent would see all six stimuli, and `Group`
would only record which came last. `design()` can tell the two apart from the
structure, but not which later branches act on the field, so check a run's
exposure history (below). If the study intends a different contrast, declare it
with `design(survey, annotations=...)`, and the summary shows your declaration
next to what the structure implies.

```python
privacy_survey = surveyir.load_qsf("tests/fixtures/qualtrics/privacy.qsf")
privacy = surveyir.design(privacy_survey)
print(privacy.summary().splitlines()[0])
print(privacy.factors[0].contrast, privacy.factors[0].recorded_field)
```

```text
FL_4: assignment to Group (arms display nothing; all run, the last one wins: each value with nominal share 1/6), 6 of 6, evenly presented
assignment ['Group']
```

What a respondent was actually shown comes from a run. `exposures(run, survey)`
derives the exposure history from the run's trace and audit: every screen
displayed, in order, and which randomizer arms each came from. It deliberately
leaves out the recorded fields, so the two can be compared:

```python
from surveyir.runtime.design import exposures

panel = {"PROLIFIC_PID": "p1", "STUDY_ID": "s1", "SESSION_ID": "x1"}  # panel fields
sim = surveyir.Simulator(privacy_survey, seed=1)
run = sim.respondent(surveyir.RandomAnswerer(seed=2), embedded=panel)
history = exposures(run, privacy_survey)
fl4 = history.factor("FL_4")
print("arms run, in order:", fl4.arms_shown_in_order)
print("screens shown inside them:", sum(len(a.displays) for a in fl4.arms))
print("recorded Group:", run.embedded["Group"])
print("blocks shown:", [privacy_survey.blocks[b].description for b, _ in history.blocks])
```

```text
arms run, in order: ('FL_34', 'FL_28', 'FL_32', 'FL_31', 'FL_29', 'FL_30')
screens shown inside them: 0
recorded Group: PSA
blocks shown: ['Introduction', 'Privacy Sandbox - A', 'Finish-1']
```

The randomizer ran all six arms, `Group` holds the last one, and the respondent
saw exactly one condition block: the one the branch on `Group` selected.

### What an arm shows

`stimulus_for_arm(survey, factor_id, arm)` answers the question per arm instead
of per run: the screens a respondent sees under that arm of a 1-of-n randomizer
and under no other, including screens chosen later by display logic or branches
on a field the arm set. In *Targeting Fairness*, arm FL_493 only sets
`Segment=2`; the vignette QID1459 is shown by display logic on `Segment`:

```python
from surveyir.runtime import stimulus_for_arm

tf = surveyir.load_qsf("tests/fixtures/qualtrics/targeting_fairness.qsf")
s = stimulus_for_arm(tf, "FL_491", "FL_493")
print([d.qid for d in s.displays], s.reasons, s.walks)
print(s.text[:110])
```

```text
['QID1459'] {('QID1459', None): 'only'} 5
A snack foods company has developed a new line of snacks. Initial testing showed that, due to the taste and te
```

It works by simulation, not by reading the flow: `walks` paired walks (5 by
default), each run once per arm with that arm forced and everything else
identical (seed, answerer, fresh balancer). A display is specific to the arm
when no other arm's walk shows it (`"only"`), or shows it only with different
rendered text or choices (`"content"`, e.g. a field the arm sets piped into a
shared screen). The limits follow from the method: logic on answers is evaluated
with the answers given (`make_answerer=lambda walk: ...`; none by default), so a
screen that depends on an answer is found only if some walk gives that answer,
and it is listed in `s.sometimes` when it is specific to the arm in some walks
but not others. A screener that ends the survey on a blank answer stops every
walk before the randomizer (`s.walks == 0`): pass an answerer such as
`ScreenerAwareAnswerer`. Walks are strict; keyword arguments go to `Simulator`.
Randomizers that show several arms to each respondent have no arm-specific
stimulus and raise `ValueError`; use `exposures(run, survey)` for those.

## Random values

Embedded data can be set to a random number, e.g. `${rand://int/1:4}`, often as
a hand-rolled assignment. These show up in `design().random_values`, and the
simulator draws them:

```python
doc = {"SurveyElements": [
    {"Element": "BL", "Payload": [{"Type": "Default", "ID": "BL_1", "BlockElements": []}]},
    {"Element": "FL", "Payload": {"Type": "Root", "Flow": [
        {"Type": "EmbeddedData", "FlowID": "FL_2", "EmbeddedData": [
            {"Field": "arm", "Type": "Custom", "Value": "${rand://int/1:4}"}]}]}},
]}
small = surveyir.load_qsf(doc)
print(surveyir.design(small).random_values)
sim = surveyir.Simulator(small, seed=3)
print([sim.respondent().embedded["arm"] for _ in range(8)])
```

```text
[RandomValue(field='arm', expression='${rand://int/1:4}', flow_id='FL_2')]
['4', '2', '4', '1', '1', '2', '2', '2']
```

## From the command line

```console
$ surveyir design tests/fixtures/qualtrics/hiring_algorithms.qsf
FL_7: exposure contrast, 1 of 2, evenly presented
  - Algorithmic Hiring (nominal share 0.5)
  - Hiring Team (nominal share 0.5)
Nominal shares are k/n, ignoring branches; not exposure probabilities, nor conditional on balancing history.
```

`surveyir design --json` writes the full structure.
