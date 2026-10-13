# Real responses

`read_responses` reads a response export into one recording per respondent:

- the randomization they got;
- their answers;
- the embedded data supplied from outside the flow (panel, recipient and URL fields).

Replaying a recording walks the survey again with those decisions and answers. Display logic, branches, skips and embedded data are then evaluated as the respondent went. This gives you:

- each respondent's path through the survey;
- which condition they were in;
- a way to check the runtime against what the platform did.

It reads Qualtrics CSV exports and LimeSurvey `.lsa`, `.lsr` and CSV exports (see [LimeSurvey](limesurvey.md#real-responses)).

## Reading an export

The example below uses an export written by the simulator, which has the same layout as a real Qualtrics CSV export.

```python
from pathlib import Path
import tempfile

import surveyir
from surveyir import ColumnOptions, RandomAnswerer, Simulator, read_responses, write_responses_csv

survey = surveyir.load("tests/fixtures/qualtrics/targeting_fairness.qsf")
people = list(Simulator(survey, seed=1, strict=False).run(5, RandomAnswerer(seed=2)))
export = write_responses_csv(survey, people, Path(tempfile.mkdtemp()) / "export.csv",
                             options=ColumnOptions(include_metadata=True))

recordings = read_responses(export, survey)
rec = recordings[0]
print(rec.id, rec.finished, rec.orders[("flow", "FL_491", None)])
print(len(rec.answers), "answers;", "inputs:", sorted(rec.embedded))
```

```text
R_sim000000 True ('FL_497',)
4 answers; inputs: []
```

Columns are matched by their ImportId. Repeated export tags therefore stay apart, and a legacy export without the ImportId row is matched by column name.

The export settings are read off the header:
- whether multi-value fields are split or joined;
- whether display order is in one column per item, in one `|`-joined column, or absent.

Answers take the shapes the simulator uses: choice ids for recodes, numbers for sliders, constant sums and ranks. `finished_only=True` drops responses the export marks unfinished.

## Replaying

```python
from surveyir import Replayer
from surveyir.responses import REPLAY_FALLBACK

replayer = Replayer(survey, strict=False, fallback_kinds=REPLAY_FALLBACK)
again = replayer.respondent(rec.orders, rec.answers, embedded=rec.embedded, drawn=rec.drawn)
print(again.flow_order == people[0].flow_order,
      set(again.state.displayed) == set(people[0].state.displayed))
```

```text
True True
```

Pass `drawn=rec.drawn`: random numbers the flow draws (a common way to assign conditions) take the respondent's exported value instead of a new draw. Some orders are never in a Qualtrics export: matrix column order, and the scale flip when no flip-scale question shows it. `REPLAY_FALLBACK` lets the replay draw them without counting them as gaps. Anything else the recording lacks is a gap. A strict replay stops at a gap; a permissive one draws it and records it in the run's audit.

## Exports without display order

When an export has no display-order columns, a recording's orders are reconstructed. LimeSurvey exports never have them.

- **Flow randomizers.** A randomizer that shows a subset of its arms is decided by searching replays (`surveyir.runtime.reconstruct`). The search tries each arm and keeps the one under which the replay displays every answered question, and under which the embedded fields the flow sets come out as exported.

  The search follows the actual flow, so it works when:
  - the same block sits under several randomizers;
  - an arm only sets embedded data that display logic reads.
- **Randomized blocks and loops.** The questions or iterations that have answers. These say what was shown, not in which order.

Everything reconstructed is listed in `Recording.inferred`.

```python
plain = write_responses_csv(survey, people, Path(tempfile.mkdtemp()) / "plain.csv",
                            options=ColumnOptions(display_order="none", include_metadata=True))
rec = read_responses(plain, survey)[0]
print(rec.orders[("flow", "FL_491", None)], rec.inferred)
```

```text
('FL_497',) (('flow', 'FL_491', None),)
```

**When the answers can't tell arms apart.** This happens with arms that show the same screens and leave no trace in the export, such as a randomizer that only sets the order of later sections. The decision is then:
- left out of the recording;
- noted in `Recording.unrecoverable`;
- drawn by the replay.

Decisions nested under it are settled under each equally good arm. Whichever arm the replay draws, the respondent's own inner conditions follow.

## What an export cannot hold

| Not in the export | What happens |
|---|---|
| Matrix column order | Drawn on replay (`REPLAY_FALLBACK`) |
| The scale flip | Read off a flip-scale question's display order when there is one; otherwise drawn (`REPLAY_FALLBACK`) |
| Random numbers the flow draws (`${rand://...}`) | Not missing: the exported value is the respondent's draw, kept in `Recording.drawn` and used by the replay (`drawn=`) instead of a new draw |
| An earlier answer to a question shown twice | Only the last answer is exported; logic that read the earlier one cannot be replayed |
| Loop order | Iterations with answers, order unknown (`inferred`); logic that reads "the last iteration" can then differ |
| Display order, when not exported | Reconstructed as above (`inferred`), or noted (`unrecoverable`) |
| Heat-map clicks, files, signatures, pick-group-rank placements | Not replayed; noted (`unrecoverable`) |
| The random seed (Qualtrics) | `Recording.seed` is None |

`scripts/validate_runtime.py` reads the real responses of the 19 fixture studies this way and replays every finished respondent; the aggregates are in `tests/fixtures/validation/twin.json` (see [simulation](simulation.md#choosers-and-replay)).
