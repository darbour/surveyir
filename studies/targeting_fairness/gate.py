"""The "execution verified" gate: was the instrument administered as specified?

Checks every simulated respondent's trace and audit against ``study.ARMS``, which
is written from the instrument. A failure here is an *execution failure*: what the
respondent model was given is not the experiment, and no comparison with humans
is meaningful until it is fixed. Whether the model's answers then resemble human
answers is a separate question (predictive discrepancy; see analysis.py).

The checks read the trace text rather than ``exposures(run).factor(...).arms``:
this study's arms only set embedded data, and the vignettes are chosen afterwards
by display logic, so ``exposures()`` attributes no screens to either arm.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace

from study import ARMS, END_NODE, FACTOR, OUTCOME

from surveyir.runtime import RespondentRun
from surveyir.runtime.trace import (
    Allowed,
    Approximation,
    Display,
    RandomizerDecision,
    Response,
)

CHECKS = {
    "one_arm": "exactly one arm of FL_491 assigned",
    "own_screen": "the assigned arm's vignette displayed exactly once",
    "no_other_screen": "the other arm's vignette never displayed (by id or by its text)",
    "outcome_after": "outcome displayed and answered after the vignette",
    "same_page_or_later": "vignette on the outcome's page or an earlier one",
    "recorded_fields": "Segment/target recorded as the arm specifies",
    "no_unallowed_approximations": "every execution-affecting approximation in the audit "
    "was explicitly allowed by the study (allow set; empty for the mock runs)",
    "not_privileged": "respondent model had no access to hidden state",
    "finished": "reached the end of the survey",
}


@dataclass
class GateResult:
    n: int = 0
    arms: Counter = field(default_factory=Counter)
    failures: dict[str, list[int]] = field(default_factory=lambda: {k: [] for k in CHECKS})
    approximations: Counter = field(default_factory=Counter)
    allowed: Counter = field(default_factory=Counter)
    allow: frozenset[str] = frozenset()
    errors: list[str] = field(default_factory=list)  # ExecutionError messages

    @property
    def passed(self) -> bool:
        return not self.errors and not any(self.failures.values())

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "passed": self.passed,
            "arms": dict(self.arms),
            "checks": {
                k: {"description": d, "failures": len(self.failures[k]),
                    "examples": self.failures[k][:5]}
                for k, d in CHECKS.items()
            },
            "approximations": dict(self.approximations),
            "allowed": dict(self.allowed),
            "allow": sorted(self.allow),
            "errors": self.errors,
        }


def arm_of(run: RespondentRun) -> str | None:
    shown = [e.shown for e in run.audit
             if isinstance(e, RandomizerDecision) and e.node_id == FACTOR]
    return shown[0][0] if len(shown) == 1 and len(shown[0]) == 1 else None


def check_run(run: RespondentRun, i: int, result: GateResult, arms: dict = ARMS) -> None:
    fail = result.failures
    displays = [o for o in run.trace if isinstance(o, Display)]
    arm_key = arm_of(run)
    result.n += 1
    result.arms[arm_key or "none"] += 1
    if arm_key is None or arm_key not in arms:
        fail["one_arm"].append(i)
        return
    arm = arms[arm_key]
    others = [a for a in arms.values() if a.key != arm_key]

    own = [d for d in displays if d.qid == arm.screen]
    if len(own) != 1 or arm.phrase not in own[0].text:
        fail["own_screen"].append(i)
    if any(d.qid == o.screen or o.phrase in d.text for d in displays for o in others):
        fail["no_other_screen"].append(i)

    outcome = [d for d in displays if d.qid == OUTCOME]
    answered = [k for k, o in enumerate(run.trace) if isinstance(o, Response) and o.qid == OUTCOME]
    if own and outcome and answered:
        vignette_pos = run.trace.index(own[0])
        if not (own[0].seq < outcome[0].seq and vignette_pos < answered[0]):
            fail["outcome_after"].append(i)
        if own[0].page > outcome[0].page:
            fail["same_page_or_later"].append(i)
    else:
        fail["outcome_after"].append(i)

    if run.embedded.get("Segment") != arm.segment or run.embedded.get("target") != arm.target:
        fail["recorded_fields"].append(i)

    approx = [e for e in run.audit if isinstance(e, Approximation)]
    result.approximations.update(f"{e.code}@{e.location} ({e.affects})" for e in approx)
    allowed = [e for e in run.audit if isinstance(e, Allowed)]
    result.allowed.update(f"{e.code}@{e.location}" for e in allowed)
    accepted = {(e.code, e.location) for e in allowed
                if e.code in result.allow or f"{e.code}:{e.location}" in result.allow}
    if any(e.execution_affecting and (e.code, e.location) not in accepted for e in approx):
        fail["no_unallowed_approximations"].append(i)
    if run.privileged:
        fail["not_privileged"].append(i)
    if not run.finished or run.ended_by != END_NODE:
        fail["finished"].append(i)


def verify(runs: list[RespondentRun], errors: list[str] | None = None,
           allow: frozenset[str] = frozenset(), arms: dict = ARMS) -> GateResult:
    result = GateResult(errors=list(errors or []), allow=frozenset(allow))
    for i, run in enumerate(runs):
        check_run(run, i, result, arms)
    return result


def swapped_arms() -> dict:
    """A deliberately wrong statement of the design (each arm expects the other's
    vignette), used as a negative control: the gate must fail on every respondent."""
    a, b = ARMS.values()
    return {
        a.key: replace(a, screen=b.screen, phrase=b.phrase),
        b.key: replace(b, screen=a.screen, phrase=a.phrase),
    }


def information_state(seen: dict[tuple[int, str], tuple[str, ...]], runs: list[RespondentRun]
                      ) -> dict[str, float]:
    """Share of respondents whose model had their own vignette in context when rating
    the outcome (and the other arm's, which must be 0). Not part of the execution
    gate: the isolated baseline is *meant* to score 0 on the first."""
    own = other = n = 0
    for i, run in enumerate(runs):
        arm_key = arm_of(run)
        if arm_key not in ARMS or (i, OUTCOME) not in seen:
            continue
        labels = seen[i, OUTCOME]
        n += 1
        own += ARMS[arm_key].label in labels
        other += any(a.label in labels for a in ARMS.values() if a.key != arm_key)
    return {"n": n, "own_vignette_in_context": own / n if n else float("nan"),
            "other_vignette_in_context": other / n if n else float("nan")}
