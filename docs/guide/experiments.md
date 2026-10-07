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
FL_42: between-subjects, 1 of 2, evenly presented if attitude value (choice 1) = 50
  - support (p=0.5): side1='a good idea', side 2='supports', side3='in support'
  - against (p=0.5): side1='a bad idea', side2='opposes', side3='against'
FL_6: between-subjects, 1 of 2, evenly presented
  - FL_7 (p=0.5): absurd_1='A'
  - FL_8 (p=0.5): absurd_1='B'
FL_15: between-subjects, 1 of 2, evenly presented
  - FL_16 (p=0.5): absurd_2='A'
  - FL_17 (p=0.5): absurd_2='B'
FL_12: between-subjects, 1 of 2, evenly presented
  - FL_13 (p=0.5): absurd_3='A'
  - FL_14 (p=0.5): absurd_3='B'
FL_50: between-subjects, 1 of 2, evenly presented
  - FL_51 (p=0.5): s_1='control'
  - FL_52 (p=0.5): s_1='test'
FL_59: between-subjects, 1 of 2, evenly presented
  - FL_60 (p=0.5): s_2='control'
  - FL_61 (p=0.5): s_2='test'
FL_56: between-subjects, 1 of 2, evenly presented
  - FL_57 (p=0.5): s_3='control'
  - FL_58 (p=0.5): s_3='test'
block_questions randomized at BL_cMZntkAHYaNDzEy (advanced)
block_questions randomized at BL_bJCKlj1xzXLsWUe (advanced)
block_questions randomized at BL_3RfCDTdyXhIpQJE (advanced)
```

Reading the summary:

- **Factors.** Each line `FL_…` is a flow randomizer. "between-subjects, 1 of 2"
  means each respondent sees one of two arms. "order, 4 of 4" would mean everyone
  sees every arm, in random order.
- **Assignments.** An arm lists the embedded-data values it sets, which is usually
  how the condition is recorded in the data (`s_1='control'`).
- **Probabilities.** `p` is the chance of an arm given that the respondent reaches
  the randomizer. Nested arms also show the overall chance.
- **Conditions.** `if …` means the randomizer only runs for respondents who meet
  that branch condition. FL_42 is reached only when `attitude` is exactly 50.
- **Within-subject order.** The `block_questions randomized` lines are question
  order randomized inside a block.

## Factors and arms

```python
for f in d.between_subjects[:3]:
    arms = ", ".join(f"{a.key} (p={a.probability})" for a in f.arms)
    print(f"{f.id}: treatment field {f.treatment_fields}, evenly presented: "
          f"{f.even_presentation}, arms: {arms}")
```

```text
FL_42: treatment field ['side1', 'side3'], evenly presented: True, arms: FL_29 (p=0.5), FL_43 (p=0.5)
FL_6: treatment field ['absurd_1'], evenly presented: True, arms: FL_7 (p=0.5), FL_8 (p=0.5)
FL_15: treatment field ['absurd_2'], evenly presented: True, arms: FL_16 (p=0.5), FL_17 (p=0.5)
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

The simulator records a note when a respondent reaches one of these. You can
emulate a web service with a hook (see [Simulating respondents](simulation.md)).

## A disguised between-subjects design

*Privacy* shows all six groups to every respondent, which looks like order
randomization. But each group sets the same field, `Group`, so the value that
sticks is the last one shown. In effect it's a 1-of-6 assignment. The real data
confirms this: every respondent's exported `Group` equals the last arm in their
display order.

```python
privacy = surveyir.design(surveyir.load_qsf("tests/fixtures/qualtrics/privacy.qsf"))
print(privacy.summary().splitlines()[0])
print(privacy.factors[0].between_subjects, privacy.factors[0].last_shown_assigns)
```

```text
FL_4: between-subjects (all shown; last arm sets Group), 6 of 6, evenly presented
True ['Group']
```

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
FL_7: between-subjects, 1 of 2, evenly presented
  - Algorithmic Hiring (p=0.5)
  - Hiring Team (p=0.5)
```

`surveyir design --json` writes the full structure.
