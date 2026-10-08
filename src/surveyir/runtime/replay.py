"""Re-administer a recorded respondent: forced randomization and forced answers.

A replay walks the survey with every randomization decision taken from a
record (``ReplayChooser``) and every answer taken from a record
(``ReplayAnswerer``). Display logic, branches, skips, carry forward and embedded
data are then evaluated by the runtime as the respondent goes, with the state at
each point, not with the final state. Replaying a real response checks the
runtime against what Qualtrics did; replaying a simulated run reproduces it.

>>> rec = record(run)                       # from a simulated run
>>> again = replay(survey, rec.orders, rec.answers, embedded=rec.embedded, seed=rec.seed)
>>> again.trace == run.trace
True

Recorded orders are keyed ``(kind, node_id, loop_id)`` (``ChoiceRequest``) and
hold the options *as presented*, in order: arm keys for a flow randomizer (the
``FL_<id>_DO`` columns), question ids for a block, choice/row ids for a question
(``"choices"``), column ids (``"columns"``), loop ids (``"loop"``).

* A recorded order may list only what was shown. When every option is presented
  (``k`` equals the number of options), options it does not list are presented
  after it, in source order, so their display logic is still evaluated. When a
  subset is presented, exactly the recorded options are.
* Recorded options the walker does not offer (hidden by choice display logic)
  are dropped.
* A missing decision is a gap (``replay.gap``): a strict replay raises
  ``ReplayGap``; with ``allow={"replay.gap"}`` (or ``strict=False``) the fallback
  chooser draws it and the gap is recorded in the run's audit. Kinds listed in
  ``fallback_kinds`` (say, ``"columns"``, which exports do not record) are drawn
  by the fallback without counting as gaps, and listed in ``ReplayChooser.fallbacks``.
* The scale flip (``("flip", "scale", None)``: ``("flipped",)`` or
  ``("normal",)``) is not in Qualtrics exports; unrecorded, it is "normal".
* One key holds one order: a block placed twice in the flow (outside a loop)
  replays the same recorded order both times, and ``record`` keeps the last.
"""

from __future__ import annotations

import random
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..model import Survey
from .execution import ExecutionError, ExecutionPolicy, approximation
from .state import Answer
from .trace import Affects, Approximation, Display, EmbeddedSet, RandomizerDecision
from .walker import (
    FLIP_OPTIONS,
    ChoiceKind,
    ChoiceRequest,
    Chooser,
    Counterbalancer,
    RespondentRun,
    ResponseContext,
    Simulator,
    default_chooser,
)

RecordKey = tuple[str, str, str | None]  # (kind, node_id, loop_id)

#: what a missing decision of each kind can change
_GAP_AFFECTS: dict[str, Affects] = {"flow": "assignment"}


class ReplayGap(ExecutionError):
    """A strict replay reached a randomization decision the record does not hold."""

    def __init__(self, approx: Approximation, request: ChoiceRequest, why: str) -> None:
        self.request = request
        super().__init__(approx)
        self.args = (
            f"replay.gap: no recorded {request.kind} order for {request.node_id}"
            + (f" (loop {request.loop_id})" if request.loop_id is not None else "")
            + f" ({why}); options {list(request.options)}. Record it under "
            f"{(request.kind, request.node_id, request.loop_id)!r}, or pass "
            "allow={'replay.gap'} (or strict=False) to draw it with the fallback chooser.",
        )

    def __str__(self) -> str:
        return str(self.args[0])


class ReplayChooser:
    """Forces recorded decisions (see the module docstring for the rules).

    ``policy`` decides gaps (strict by default: raise ``ReplayGap``); ``Replayer``
    and ``replay`` pass the simulator's. Gaps drawn by ``fallback`` are listed in
    ``gaps`` (as approximations) or, for ``fallback_kinds``, in ``fallbacks``.
    """

    def __init__(
        self,
        recorded: Mapping[RecordKey, Sequence[str]] | None = None,
        *,
        fallback: Chooser = default_chooser,
        fallback_kinds: Collection[ChoiceKind] = (),
        policy: ExecutionPolicy | None = None,
    ) -> None:
        self.recorded: dict[RecordKey, tuple[str, ...]] = {
            k: tuple(v) for k, v in (recorded or {}).items()}
        self.fallback = fallback
        self.fallback_kinds = frozenset(fallback_kinds)
        self.policy = policy or ExecutionPolicy()
        self.gaps: list[Approximation] = []
        self.fallbacks: list[ChoiceRequest] = []

    def choose(
        self, req: ChoiceRequest, rng: random.Random, balancer: Counterbalancer
    ) -> tuple[Sequence[str], Mapping[str, float] | None]:
        if req.kind == "flip":
            rec = self.recorded.get((req.kind, req.node_id, req.loop_id))
            return (rec[0] if rec and rec[0] in req.options else FLIP_OPTIONS[0],), None
        if not req.options or req.k == 0:
            return (), None
        rec = self.recorded.get((req.kind, req.node_id, req.loop_id))
        if rec is None:
            if len(req.options) == 1:  # nothing to decide
                return req.options, None
            return self._missing(req, rng, balancer, "nothing recorded")
        offered = set(req.options)
        shown = list(dict.fromkeys(o for o in rec if o in offered))
        if rec and not shown:
            return self._missing(
                req, rng, balancer,
                f"recorded {list(rec)}, none of which is offered")
        if req.k >= len(req.options):  # everything is presented: unlisted ones follow
            shown += [o for o in req.options if o not in shown]
        shown = shown[: req.k]
        if req.even:  # keep the shared counts as the recorded history left them
            counts = balancer.counts.setdefault(req.balance_key, {})
            for o in shown:
                counts[o] = counts.get(o, 0) + 1
        if req.reverse:  # the walker reverses it back into the presented order
            shown.reverse()
        return tuple(shown), None

    def _missing(
        self, req: ChoiceRequest, rng: random.Random, balancer: Counterbalancer, why: str
    ) -> tuple[Sequence[str], Mapping[str, float] | None]:
        if req.kind in self.fallback_kinds:
            self.fallbacks.append(req)
            return self.fallback.choose(req, rng, balancer)
        where = f"{req.kind}:{req.node_id}" + (f"#{req.loop_id}" if req.loop_id else "")
        approx = approximation(
            "replay.gap", where,
            f"replay has no usable {req.kind} order for {req.node_id} ({why}); drawn by "
            "the fallback chooser",
            _GAP_AFFECTS.get(req.kind, "exposure"))
        if not self.policy.permits(approx):
            raise ReplayGap(approx, req, why)
        if approx not in self.gaps:
            self.gaps.append(approx)
        return self.fallback.choose(req, rng, balancer)


class ReplayAnswerer:
    """Answers each displayed question with its recorded answer.

    ``answers`` maps ``(qid, loop_id)`` to an ``Answer`` or a raw value (as in
    ``Answer.value``). Questions with no recorded answer are left blank.
    """

    __respondent_protocol__ = 2

    def __init__(self, answers: Mapping[tuple[str, str | None], Any]) -> None:
        self.answers = dict(answers)

    def answer(self, ctx: ResponseContext) -> Any:
        a = self.answers.get((ctx.view.qid, ctx.view.loop_id))
        if a is None:
            return None
        if isinstance(a, Answer):
            return Answer(value=a.value, text=dict(a.text))
        return Answer(value=a)


@dataclass
class Recording:
    """What a replay needs from one respondent (see ``record``)."""

    orders: dict[RecordKey, tuple[str, ...]]
    answers: dict[tuple[str, str | None], Answer]
    embedded: dict[str, str] = field(default_factory=dict)
    seed: int | None = None


def record(run: RespondentRun) -> Recording:
    """The decisions, answers, supplied embedded data and seed of a run, to replay it.

    Orders come from the audit's ``RandomizerDecision``s (as presented), the flip
    from the trace (any flipped display), and embedded data from what was
    supplied for the respondent.
    """
    orders: dict[RecordKey, tuple[str, ...]] = {}
    for e in run.audit:
        if isinstance(e, RandomizerDecision):
            orders[(e.kind, e.node_id, e.loop_id)] = tuple(e.shown)
    if any(isinstance(ob, Display) and ob.flipped for ob in run.trace):
        orders[("flip", "scale", None)] = (FLIP_OPTIONS[1],)
    embedded = {e.name: e.value for e in run.audit
                if isinstance(e, EmbeddedSet) and e.source == "respondent"}
    answers = {k: Answer(value=a.value, text=dict(a.text)) for k, a in run.answers.items()}
    return Recording(orders, answers, embedded, run.seed)


class Replayer:
    """Replays respondents of one survey through one ``Simulator``.

    Keyword arguments go to ``Simulator`` (``strict``, ``allow``,
    ``implementations``, ``seed``, ...); ``fallback`` and ``fallback_kinds`` to
    ``ReplayChooser``. Quota counts and balancer counts carry over between
    respondents, as they would in the field.
    """

    def __init__(
        self,
        survey: Survey,
        *,
        fallback: Chooser = default_chooser,
        fallback_kinds: Collection[ChoiceKind] = (),
        **simulator_kw: Any,
    ) -> None:
        self.chooser = ReplayChooser(fallback=fallback, fallback_kinds=fallback_kinds)
        self.simulator = Simulator(survey, chooser=self.chooser, **simulator_kw)
        self.chooser.policy = self.simulator.policy

    def respondent(
        self,
        orders: Mapping[RecordKey, Sequence[str]],
        answers: Mapping[tuple[str, str | None], Any],
        *,
        embedded: Mapping[str, str] | None = None,
        seed: int | None = None,
    ) -> RespondentRun:
        """Replay one respondent. Raises ``ExecutionError`` (``ReplayGap`` for a
        missing decision) where a strict replay cannot proceed; the partial run is
        on the error's ``run``."""
        chooser = self.chooser
        chooser.recorded = {k: tuple(v) for k, v in orders.items()}
        chooser.gaps, chooser.fallbacks = [], []
        run = self.simulator.respondent(
            ReplayAnswerer(answers), embedded=dict(embedded or {}), seed=seed)
        if chooser.gaps:  # allowed or permissive gaps, recorded like any approximation
            audit = list(run.audit)
            for approx in chooser.gaps:
                allowed = self.simulator.policy.handle(approx)
                if approx not in run.state.approximations:
                    run.state.approximations.append(approx)
                    run.state.note(approx.detail)
                    audit.append(approx)
                if allowed is not None and allowed not in audit:
                    audit.append(allowed)
            run.audit = tuple(audit)
        return run


def replay(
    survey: Survey,
    recorded_orders: Mapping[RecordKey, Sequence[str]],
    answers: Mapping[tuple[str, str | None], Any],
    *,
    embedded: Mapping[str, str] | None = None,
    seed: int | None = None,
    fallback: Chooser = default_chooser,
    fallback_kinds: Collection[ChoiceKind] = (),
    **simulator_kw: Any,
) -> RespondentRun:
    """Replay one respondent: recorded orders, recorded answers, and recorded inputs
    (``embedded=``: panel, recipient or URL fields).

    Strict by default, like ``Simulator``: a gap that affects execution (a missing
    decision, unrun JavaScript, an uncalled web service) raises ``ExecutionError``
    unless it is implemented or allowed. ``seed`` fixes the respondent's random
    streams (fallback draws, ``rand://`` text); pass the original run's seed to
    reproduce it.
    """
    return Replayer(survey, fallback=fallback, fallback_kinds=fallback_kinds,
                    **simulator_kw).respondent(
        recorded_orders, answers, embedded=embedded, seed=seed)


__all__ = [
    "Recording",
    "ReplayAnswerer",
    "ReplayChooser",
    "ReplayGap",
    "Replayer",
    "record",
    "replay",
]
