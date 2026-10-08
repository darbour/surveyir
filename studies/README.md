# Studies: end-to-end experimental examples

Each directory here is one complete simulated replication of a real survey
experiment, built on surveyir but kept outside the package. Estimators,
calibration and respondent models live here, never in `src/`. A study contains:

1. the original instrument (`survey.qsf`);
2. the preflight: `surveyir check --strict` and the `design()` summary;
3. verified exposure traces for every arm, with a programmatic gate;
4. simulated responses from a pluggable respondent model, as a Qualtrics-shaped CSV;
5. the original analysis, run on the human data and on each simulated dataset;
6. a generated `report.md` comparing treatment contrasts with the human estimates.

The report keeps two kinds of problem apart:

- **Execution failures**: the instrument was not administered as specified. The
  evidence comes from strict mode, the audit and the traces.
- **Predictive discrepancies**: the instrument was administered correctly, but the
  simulated answers differ from the human ones.

The report also compares the full-context model with a deliberately simplified
**isolated** baseline: the same model, asked each question on its own, without the
instrument's history or treatment screens.

## `targeting_fairness/`

```console
uv run python studies/targeting_fairness/run.py                       # mock + isolated-mock
uv run python studies/targeting_fairness/run.py --model mock
uv run python studies/targeting_fairness/run.py --model isolated-mock
```

Each condition takes a few seconds and makes no network calls. The run needs
two things:

- **The human responses.** These are not redistributed. Set `SURVEYIR_TWIN_DAT`
  to the mega-study's `.dat` directory (the variable surveyir's validation tests
  use); it defaults to `~/Code/Twin-2K-500-Mega-Study/.dat`.
- **The original analysis script (optional).** If a checkout of
  [Twin-2K-500-Mega-Study](https://github.com/TianyiPeng/Twin-2K-500-Mega-Study)
  with its `.venv` is found (`TWIN_MEGA_STUDY`, default
  `~/Code/Twin-2K-500-Mega-Study`), its unmodified
  `mega_study_evaluation/targeting_fairness/mega_study_evaluation.py` is also run
  on the human CSV and each simulated CSV. Its output goes to a temporary
  directory, because that output includes individual-level human data.

  Without the checkout, `analysis.py` (a stdlib port of the same estimator)
  gives the estimates on its own. The port is checked against the original
  script's published human value, d = −0.5436, and the two agree to all printed
  digits.

The exit status is non-zero if any condition fails the exposure gate, or if a
negative control is not caught.

| file | role |
|---|---|
| `survey.qsf` | the original QSF (identical to the mega-study's `.dat/targeting_fairness/raw_data/survey.qsf` and `tests/fixtures/qualtrics/targeting_fairness.qsf`) |
| `study.py` | the design as read from the instrument: arms, their treatment screens and distinguishing text, the outcome, data locations |
| `respondents.py` | `MockRespondent`, `IsolatedBaseline`, `LLMRespondent`, `PromptMeter` (all `answer(ctx)`) |
| `gate.py` | the exposure gate, the information-state measure, and the negative-control design |
| `analysis.py` | the original estimator (ported), the oriented contrast with CIs, and the runner for the original script |
| `report.py` | writes `report.md` from `outputs/` |
| `run.py` | the CLI |
| `outputs/` | `check.txt`, `design.txt`/`.json`, `negative_controls.json`, and per condition `responses.csv`, `results.json` and `traces/` (one respondent per arm: visible transcript, `exposures()` record, and the text the model saw when it answered the outcome) |
| `report.md` | the generated report |

### What the gate verifies

The gate checks every simulated respondent; the checks are listed in `gate.CHECKS`:

- exactly one arm of the randomizer `FL_491` was drawn;
- that arm's vignette was displayed exactly once;
- the other arm's vignette was never displayed, checked both by question id and
  by its distinguishing sentence anywhere in the displayed text;
- the outcome `fair1` was displayed and answered after the vignette, on the
  vignette's page or a later one;
- `Segment` and `target` were recorded as the arm specifies;
- the audit has no execution-affecting approximation that the study didn't
  explicitly allow (the mock runs allow nothing);
- the respondent model was not privileged (it had no access to hidden state);
- the respondent reached the survey's end node;
- strict mode raised no `ExecutionError`.

Two negative controls show the gate can fail:

- the real runs, checked against a deliberately swapped design statement;
- a legacy `(view, state)` answerer, which is privileged.

The gate checks administration, not what the model was told. A separate
*information-state* measure records whether the respondent model actually had its
own arm's vignette in context when it rated the outcome. The isolated baseline
scores 0% on that measure, by design, while still passing the gate.

### Respondent models

- `mock`: deterministic, with no API. It draws a seeded 3–7 rating and adds ±1
  when it can read the broad or the targeted vignette. The planted contrast is
  therefore +2 when the treatment reaches the model, and 0 when it doesn't.
- `isolated-mock`: the same mock, wrapped in `IsolatedBaseline`, which replaces
  `ctx.history` and `ctx.page` with the question alone. The simulator seed is the
  same, so the assignments and traces are identical; only the model's context
  differs.
- `claude` / `isolated-claude`: `LLMRespondent`, which wraps
  `examples/llm_respondents.ClaudeRespondent` with one conversation per respondent.
  It is never run by default (see below).

### Real LLM runs (require approval)

```console
uv run python studies/targeting_fairness/run.py --model claude            # estimate only, exits 2
uv run --with anthropic python studies/targeting_fairness/run.py \
    --model claude --i-approve-spend [--price-in USD --price-out USD]     # after approval
```

Without `--i-approve-spend`, `--model claude` prints a cost estimate and exits
without calling anything. The estimate is built in three steps:

1. **Measure the prompts.** `PromptMeter` measures the exact requests
   `ClaudeRespondent` would send. On a mock run of the same instrument, it records
   the system prompt, the conversation built from `ctx.history` and the JSON schema
   for every question the example answers.
2. **Convert to tokens.** Characters are divided by 4 (assumed). Each answer is
   assumed to produce 20 output tokens, with no prompt caching.
3. **Price and scale.** The token counts are multiplied by the number of
   respondents and priced from `ASSUMED_PRICES` in `run.py`.

The estimate that `--model claude` prints extrapolates from the first
`--meter-n` (default 50) respondents. The table in `report.md` is measured on the
full mock run. The two figures therefore differ slightly; that isn't a bug.

The prices in `ASSUMED_PRICES` are placeholders, not looked up. Replace them, or
pass `--price-in`/`--price-out`, with current published prices before approving.

An approved run also needs `ANTHROPIC_API_KEY`. It allows `answer.invalid` (a
model may break a question's validation) and records it in the gate.

The attention check shows its numbers only in an image, which the model cannot
see. Nothing branches on that answer, so it doesn't affect routing.

## Why Targeting Fairness

The criteria were:

1. a between-subjects treatment delivered as text;
2. human responses available locally (`Twin-2K-500-Mega-Study/.dat/<study>/raw_data/response.csv`, present for all 19);
3. an original analysis whose treatment estimate can be reproduced on the human data;
4. a QSF that runs under `surveyir check --strict` without `allow` or `implementations`.

Every original script in the mega-study (`mega_study_evaluation/<study>/mega_study_evaluation.py`)
computes the same per-DV statistics. Where a condition variable exists, these
include Cohen's d between conditions for humans and for twins.

| study | design (`surveyir design`) | strict check | original contrast | verdict |
|---|---|---|---|---|
| **targeting_fairness** | `FL_491`: 1 of 2. Display logic then shows one of two text vignettes, on the outcome's page | passes | Cohen's d of `fair1` by `Segment`; published human d = −0.5436, reproduced exactly | **chosen**: a single text treatment, a single outcome, n = 357, and strict execution with no gaps |
| hiring_algorithms | `FL_7`: 1 of 2 (algorithmic vs team hiring) | passes | Treatment is crossed with four jobs, the arm direction flips per job, and there are 32 matrix DVs | runnable, but the contrast is a per-job recode rather than one treatment effect; second choice |
| default_eric | 2×2: topic order (`FL_344`) × opt-in/opt-out default, in nested randomizers | blocked: JavaScript at QID631 (exposure) | opt-in vs opt-out contrasts (`condition_green`, `condition_organ`) | a strong candidate, but it needs a justified JavaScript implementation or `allow` first, and its treatment is the default, not a vignette |
| junk_fees | Fee-type order (`FL_143`, 6 of 6) with nested item randomizers | passes | no condition variable (`condition_vars = [""]`) | no between-subjects treatment contrast |
| obedient_twins | 7 factors, including a branch-gated `FL_42` and absurd/test manipulations | passes | per-DV conditions, with several factors and a branch-gated arm | runnable, but multi-factor; the `side 2` field bug (see `docs/guide/experiments.md`) complicates a first example |
| accuracy_nudges | `FL_158`: 4 conditions × nested real/fake headline arms, plus `FL_167`, a 16-headline order randomizer | passes | d by `Condition` over 16 headline DVs | runnable, but the treatment is mixed with stimulus sampling; too large for a first example |
| story_beliefs | `FL_90`: 1 of 2, with nested randomizers | passes | no condition variable in the script | no reproducible treatment estimate |
| privacy | `FL_4`: an assignment recorded in `Group` (6 of 6); a branch shows the block | passes | d by `Group` | the recorded field is not the exposure; a good second example, but more complex |
| affective_priming, digital_certification, consumer_minimalism | 1 of 4 / 1 of 2 factors, plus order randomizers | pass | d by `cond` / `item`, or none | multi-factor or order-confounded |
| recommendation_algorithms, promiscuous_donors, context_effects, idea_evaluation | randomization or exposure in question JavaScript | blocked | n/a | need JavaScript implementations |
| infotainment, preference_redistribution, quantitative_intuition | no between-subjects factor | passes | none | no treatment |

Targeting Fairness also turned out to illustrate the review's main point. The
embedded field `target`, which an analyst would naturally use to label the arms,
is the reverse of what each arm displays: `target='female'` shows the
"advertise broadly" vignette. Only the exposure traces reveal this. The original
analysis conditions on `Segment`, so its estimate is unaffected. Its d still
takes its sign from the order of rows in the file, so the harness reports the
contrast oriented by what was displayed.
