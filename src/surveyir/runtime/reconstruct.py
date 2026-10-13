"""Recover randomizer decisions an export did not record, by searching replays.

An export without display-order columns (or a LimeSurvey export, which never has
them) does not say which arm of a flow randomizer a respondent got. The answers
usually do: an arm leads to its own questions, or sets embedded data that display
logic and the exported embedded fields reveal. ``reconstruct`` finds out by
replaying the respondent under candidate decisions and keeping the ones that fit:

* a replay's misfit counts the answered questions it does not display and the
  exported embedded fields the flow sets that come out different
  (``Recording.outputs``). Questions displayed but blank do not count: a blank can
  be a skip;
* only randomizers that present a subset of their arms (``k < n``) and that the
  replay reaches are decided. A randomizer that presents every arm needs no
  decision (its order is unknown and replay draws it);
* decisions are made in walk order, each by the least misfit with the later ones
  at their defaults, then improved one at a time (with the decisions they lead to
  redone) until nothing changes. The cost grows with the number of randomizers,
  not with the number of their combinations;
* a decision is recorded (and listed in ``Recording.inferred``) when one choice
  fits best. When several fit equally (arms with the same screens and no exported
  trace, such as an arm that only orders later sections), it is left out and
  noted in ``Recording.unrecoverable``, so a replay draws it and says so; the
  decisions under it are settled under each equally good choice and recorded
  when they agree, so whichever arm a replay draws, the respondent's inner
  conditions follow. Those are listed in ``Recording.conditional``: the respondent
  reached only one of them, and which is not known.

The same block can sit under several randomizers or arms: replays walk the
actual flow, so which placement showed a block follows from the decisions.

Each replay is a fresh ``Simulator`` (no quota or balancer history); a search
that runs past ``max_replays`` recovers nothing and says so.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..model import Survey, walk
from ..model.question import NON_RESPONSE_KINDS
from .execution import ExecutionError
from .replay import Recording, RecordKey, ReplayAnswerer, ReplayChooser
from .walker import ChoiceKind, ChoiceRequest, Counterbalancer, RespondentRun, Simulator

__all__ = ["reconstruct"]

#: a subset randomizer with at most this many possible subsets is searched in full;
#: a larger one by swaps (see ``reconstruct``)
EXHAUSTIVE = 64

_ALL_KINDS: tuple[ChoiceKind, ...] = ("flow", "block", "choices", "columns", "loop", "flip")


class _Probe(ReplayChooser):
    """Recorded orders, plus trial decisions; unrecorded subset decisions of the
    searched kinds are listed in ``open`` (and take their first options meanwhile)."""

    def __init__(
        self,
        recorded: Mapping[RecordKey, Sequence[str]],
        trial: Mapping[RecordKey, tuple[str, ...]],
        kinds: Collection[str],
    ) -> None:
        super().__init__(recorded, fallback_kinds=_ALL_KINDS, touch_balancer=False)
        self.trial = dict(trial)
        self.kinds = frozenset(kinds)
        self.open: list[ChoiceRequest] = []
        self.seen: list[ChoiceRequest] = []

    def choose(
        self, req: ChoiceRequest, rng: random.Random, balancer: Counterbalancer
    ) -> tuple[Sequence[str], Mapping[str, float] | None]:
        key = (req.kind, req.node_id, req.loop_id)
        if req.kind in self.kinds and key not in self.recorded and 0 < req.k < len(req.options):
            shown = list(self.trial.get(key) or req.options[: req.k])
            self.seen.append(req)
            if key not in self.trial:
                self.open.append(req)
            if req.reverse:  # the walker reverses it back into the presented order
                shown.reverse()
            return tuple(shown), None
        return super().choose(req, rng, balancer)


def _score(survey: Survey, run: RespondentRun, rec: Recording) -> tuple[int, int]:
    answered = {k for k, a in rec.answers.items() if a.value not in (None, "", [], {}) or a.text}
    displayed = set(run.state.displayed)
    misfit = len(answered - displayed) + sum(
        1 for name, value in rec.outputs.items() if value != "" and run.embedded.get(name) != value
    )
    responding = {
        k
        for k in displayed
        if (q := survey.questions.get(k[0])) is not None and q.kind not in NON_RESPONSE_KINDS
    }
    return misfit, len(responding - answered)


Trial = dict[RecordKey, tuple[str, ...]]


def reconstruct(
    survey: Survey,
    rec: Recording,
    *,
    kinds: Collection[str] = ("flow",),
    max_replays: int = 2000,
    **simulator_kw: Any,
) -> Recording:
    """``rec`` with the unrecorded decisions of ``kinds`` its answers pin down (see
    the module docstring). Keyword arguments go to each replay's ``Simulator``
    (``implementations``, ``allow``, ...); replays are permissive."""
    simulator_kw = {"strict": False, **simulator_kw}
    cache: dict[tuple, tuple[int, list[ChoiceRequest], list[ChoiceRequest]]] = {}
    budget = [max_replays]

    def run(trial: Trial) -> tuple[int, list[ChoiceRequest], list[ChoiceRequest]]:
        """(misfit, undecided requests, every searched request reached), in walk order."""
        key = tuple(sorted(trial.items()))
        if key not in cache:
            if budget[0] <= 0:
                raise _OutOfReplays
            budget[0] -= 1
            probe = _Probe(rec.orders, trial, kinds)
            sim = Simulator(survey, chooser=probe, **simulator_kw)
            try:
                walked = sim.respondent(
                    ReplayAnswerer(rec.answers),
                    embedded=dict(rec.embedded),
                    seed=0,
                    drawn=rec.drawn,
                )
            except ExecutionError as e:
                walked = e.run
            misfit = _score(survey, walked, rec)[0] if walked is not None else 1 << 30
            cache[key] = (misfit, probe.open, probe.seen)
        return cache[key]

    def candidates(req: ChoiceRequest, trial: Trial) -> list[tuple[str, ...]]:
        """The subsets of ``req`` worth scoring: all of them when there are few;
        otherwise the best one a swap search finds (swap a shown arm for an unshown
        one while the misfit falls), and the swaps that fit as well."""
        k, options, size = _key(req), list(req.options), req.k
        if math.comb(len(options), size) <= EXHAUSTIVE:
            return list(itertools.combinations(options, size))
        current = list(trial.get(k) or options[:size])
        best = run({**trial, k: tuple(current)})[0]
        # an arm no swap improves on holds no answered question the others miss: it
        # is not tried again (arms add what they display, so this rarely changes)
        useless: set[str] = set()
        improved = True
        while improved and best > 0:
            improved = False
            for o in options:
                if o in current or o in useless:
                    continue
                for i in range(size):
                    cand = [*current[:i], o, *current[i + 1 :]]
                    m = run({**trial, k: tuple(cand)})[0]
                    if m < best:
                        current, best, improved = cand, m, True
                        break
                else:
                    useless.add(o)
        ties = [
            tuple([*current[:i], o, *current[i + 1 :]])
            for i in range(size)
            for o in options
            if o not in current
            and run({**trial, k: tuple([*current[:i], o, *current[i + 1 :]])})[0] == best
        ]
        return [tuple(current), *ties]

    def complete(trial: Trial) -> tuple[Trial, int]:
        """Decide the undecided requests in walk order, each by the best misfit with
        the later ones at their defaults."""
        trial = dict(trial)
        while True:
            misfit, undecided, _ = run(trial)
            if not undecided:
                return trial, misfit
            req = undecided[0]
            k = _key(req)
            trial[k] = min(candidates(req, trial), key=lambda o: run({**trial, k: o})[0])

    conditional: set[RecordKey] = set()

    def settle(trial: Trial, within: set[str] | None) -> tuple[Trial, list[str]]:
        """The decisions reached under ``trial`` (only those under the nodes
        ``within``, if given) that the response pins down, and notes on the rest.

        A decision the response cannot tell apart is left out; the decisions under
        it are settled under each equally good choice, so a replay that draws any of
        them still follows the respondent's inner conditions."""
        decided: Trial = {}
        notes: list[str] = []
        skip: set[str] = set()
        for req in run(trial)[2]:
            k = _key(req)
            if k[1] in skip or (within is not None and k[1] not in within):
                continue
            scored = [(complete({**trial, k: o}), o) for o in candidates(req, trial)]
            best = min(m for (_, m), _ in scored)
            fits = [(t, o) for (t, m), o in scored if m == best]
            if len(fits) == 1:
                decided[k] = fits[0][1]
                continue
            arms = sorted({a for _, o in fits for a in o})
            notes.append(
                f"{k[0]} {k[1]}: the response fits {', '.join(arms)} equally; not recovered"
            )
            below = _below(survey, k[1])
            skip |= below
            inner: dict[RecordKey, set[tuple[str, ...]]] = {}
            for t, _ in fits:
                sub, sub_notes = settle(t, below)
                for key, value in sub.items():
                    inner.setdefault(key, set()).add(value)
                notes += [n for n in sub_notes if n not in notes]
            for key, values in inner.items():
                if len(values) == 1:
                    decided[key] = next(iter(values))
                    conditional.add(key)  # holds only if the respondent reached it
                else:
                    notes.append(f"{key[0]} {key[1]}: depends on {k[1]}; not recovered")
        return decided, notes

    notes = list(rec.unrecoverable)
    try:
        trial, misfit = complete({})
        for _ in range(3):  # improve one decision at a time until nothing changes
            changed = False
            for req in run(trial)[2]:
                k = _key(req)
                scored = [(complete({**trial, k: o}), o) for o in candidates(req, trial)]
                best = min(m for (_, m), _ in scored)
                if best < misfit:
                    trial, misfit = next(x for x, _ in scored if x[1] == best)
                    changed = True
            if not changed:
                break
    except _OutOfReplays:
        notes.append(f"search stopped after {max_replays} replays; nothing recovered")
        return replace(rec, unrecoverable=tuple(notes))
    try:
        decided, unsettled = settle(trial, None)
    except _OutOfReplays:
        notes.append(f"search stopped after {max_replays} replays; nothing recovered")
        return replace(rec, unrecoverable=tuple(notes))
    orders = {**rec.orders, **decided}
    inferred = [*rec.inferred, *decided]
    notes += unsettled
    return replace(
        rec,
        orders=orders,
        inferred=tuple(inferred),
        unrecoverable=tuple(notes),
        conditional=(*rec.conditional, *sorted(conditional, key=str)),
    )


class _OutOfReplays(Exception):
    pass


def _below(survey: Survey, node_id: str) -> set[str]:
    """Ids of the flow nodes under ``node_id``."""
    node = next((n for n in survey.walk_flow() if getattr(n, "id", None) == node_id), None)
    children = getattr(node, "children", None) or []
    return {n.id for n in walk(children) if getattr(n, "id", None)}


def _key(req: ChoiceRequest) -> RecordKey:
    return (req.kind, req.node_id, req.loop_id)
