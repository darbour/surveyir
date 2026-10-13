"""Recover randomizer decisions an export did not record, by searching replays.

An export without display-order columns (or a LimeSurvey export, which never has
them) does not say which arm of a flow randomizer a respondent got. The answers
usually do: an arm leads to its own questions, or sets embedded data that display
logic and the exported embedded fields reveal. ``reconstruct`` finds out by
replaying the respondent under candidate decisions and keeping the ones that fit:

* a replay's misfit counts the answered questions it does not display and the
  exported embedded fields the flow sets that come out different, a blank included
  (``Recording.outputs``). Questions displayed but blank do not count: a blank can
  be a skip;
* only randomizers that present a subset of their arms (``k < n``) and that the
  replay reaches are decided. A randomizer that presents every arm needs no
  decision (its order is unknown and replay draws it);
* decisions are made in walk order, each by the least misfit with the later ones
  at their defaults, then improved one at a time (with the decisions they lead to
  redone) until nothing changes, and, if the replay still misfits, two at a time
  (an arm of one may set a field another's arm reads). The cost grows with the
  number of randomizers, not with the number of their combinations;
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

Two more things an export leaves out are searched the same way:

* **earlier answers to a question displayed more than once** (its block placed
  twice in the flow, as in "try another round?" or a rating asked before and
  after): the export keeps only the last answer, so each earlier display is a
  decision among candidate answers: its choices (or none), and, for numbers, the
  values the survey's logic compares that question with (one either side, and the
  question's bounds), varied one part at a time. One that fits best is recorded in
  ``Recording.earlier``: the answers that follow show which way the respondent
  went. For a number this is a value on the right side of every threshold the
  logic uses, not necessarily the one they gave, which no export holds;
* **orders the export does not hold** (a randomized loop's order, a randomized
  block's order without its ``_DO`` column): these are improved one step at a time
  (an item moved, or two swapped) while the misfit falls, then while the questions displayed but
  left blank do (all orders are alike beforehand, and a blank the order explains,
  because the question was not shown, is likelier than a skip). Logic that reads
  another iteration's answer, for one, then follows the respondent.

Whatever still differs after the search is noted in ``Recording.unrecoverable``.

Each replay is a fresh ``Simulator`` (no quota or balancer history); a search
that runs past ``max_replays`` recovers nothing and says so.
"""

from __future__ import annotations

import itertools
import json
import math
import random
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

from pydantic import BaseModel

from ..model import Survey, walk
from ..model.logic import Comparison, Const, Expression, Var, iter_nodes
from ..model.question import NON_RESPONSE_KINDS
from .execution import ExecutionError
from .replay import Recording, RecordKey, ReplayAnswerer, ReplayChooser
from .trace import RandomizerDecision
from .walker import (
    ChoiceKind,
    ChoiceRequest,
    Counterbalancer,
    RespondentRun,
    ResponseContext,
    Simulator,
)

__all__ = ["reconstruct"]

#: a subset randomizer with at most this many possible subsets is searched in full;
#: a larger one by swaps (see ``reconstruct``)
EXHAUSTIVE = 64

_ALL_KINDS: tuple[ChoiceKind, ...] = ("flow", "block", "choices", "columns", "loop", "flip")
#: order kinds an export may not hold, searched by swaps when the replay misfits
_ORDER_KINDS = ("loop", "block")
#: decision kinds that change what is displayed: a recording keeps them all
_PATH_KINDS = ("flow", "block", "loop")
#: the decision kind of an earlier answer to a question displayed more than once
ANSWER = cast(ChoiceKind, "answer")


class _Probe(ReplayChooser):
    """Recorded orders, plus trial decisions; unrecorded subset decisions of the
    searched kinds are listed in ``open`` (and take their first options meanwhile)."""

    def __init__(
        self,
        recorded: Mapping[RecordKey, Sequence[str]],
        trial: Mapping[RecordKey, tuple[str, ...]],
        kinds: Collection[str],
        open_: list[ChoiceRequest],
        seen: list[ChoiceRequest],
        unordered: list[ChoiceRequest],
    ) -> None:
        super().__init__(recorded, fallback_kinds=_ALL_KINDS, touch_balancer=False)
        self.trial = dict(trial)
        self.kinds = frozenset(kinds)
        self.open, self.seen, self.unordered = open_, seen, unordered

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
        if req.kind in self.kinds and key not in self.recorded and len(req.options) > 1:
            # every arm is shown: only the order is unknown (it matters when the arms
            # set the same field, the last one winning): searched like other orders
            shown = list(self.trial.get(key) or req.options)
            self.unordered.append(req)
            if req.reverse:
                shown.reverse()
            return tuple(shown), None
        return super().choose(req, rng, balancer)


class _ProbeAnswerer(ReplayAnswerer):
    """Recorded answers, plus trial answers for earlier displays of a question shown
    more than once; each such earlier display is listed in ``open`` (and keeps the
    recorded answer meanwhile), with the candidate answers ``_candidates`` gives."""

    def __init__(
        self,
        survey: Survey,
        rec: Recording,
        trial: Mapping[RecordKey, tuple[str, ...]],
        open_: list[ChoiceRequest],
        seen: list[ChoiceRequest],
        constants: Mapping[str, set[str]],
        placements: Mapping[str, int],
    ) -> None:
        earlier = {
            (k[1], _loop(k), _nth(k)): json.loads(v[0]) for k, v in trial.items() if k[0] == ANSWER
        }
        super().__init__(rec.answers, {**rec.earlier, **earlier})
        self.survey, self.rec, self.trial = survey, rec, trial
        self.open, self.seen, self.constants = open_, seen, constants
        self.placements = placements
        self.known = set(rec.earlier)
        self.opened: set[tuple[str, str | None, int]] = set()

    def answer(self, ctx: ResponseContext) -> Any:
        n = self.occurrence(ctx)
        view = ctx.view
        # a display that another can follow (its block is placed again later), or one
        # that another did follow: its answer may be one the export overwrote
        if n < self.placements.get(view.qid, 1) - 1:
            self._open(view, n)
        if n >= 1:
            self._open(view, n - 1)
        return super().answer(ctx)

    def unused(self) -> int:
        """Trial answers for displays that never happened, or that no later display
        followed (the export would show them, not the recorded answer)."""
        out = 0
        for k, v in self.trial.items():
            if k[0] != ANSWER:
                continue
            recorded = self.rec.answers.get((k[1], _loop(k)))
            if json.loads(v[0]) == getattr(recorded, "value", None):
                continue  # the recorded answer: right whether or not a display follows
            shown = len(self.displays.get((k[1], _loop(k)), []))
            out += shown <= _nth(k) + 1
        return out

    def _open(self, view: Any, n: int) -> None:
        if (view.qid, view.loop_id, n) in self.known | self.opened:
            return
        self.opened.add((view.qid, view.loop_id, n))
        options = _candidates(
            self.survey.questions.get(view.qid),
            view,
            self.rec.answers.get((view.qid, view.loop_id)),
            self.constants.get(view.qid, set()),
        )
        if len(options) > 1:
            req = ChoiceRequest(ANSWER, view.qid, options, 1, loop_id=f"{view.loop_id or ''}@{n}")
            self.seen.append(req)
            if _key(req) not in self.trial:
                self.open.append(req)


#: most candidate answers tried for one earlier display
MAX_ANSWERS = 64


def _numbers(constants: set[str], q: Any) -> list[float]:
    """Numbers worth trying for a numeric answer: each constant the logic compares it
    with, one either side, and the question's bounds."""
    out: set[float] = set()
    for c in constants:
        try:
            x = float(c)
        except ValueError:
            continue
        out |= {x - 1, x, x + 1}
    for bound in (getattr(q, "min", None), getattr(q, "max", None)):
        if bound is not None:
            out.add(float(bound))
    return sorted(out)


def _num(x: float) -> int | float:
    return int(x) if x.is_integer() else x


def _candidates(q: Any, view: Any, final: Any, constants: set[str]) -> tuple[str, ...]:
    """Candidate answers (JSON) for an earlier display of ``q``: the shapes its
    answers take, varied one part at a time from the recorded (last) answer, over
    the values its question's logic can tell apart."""
    value = getattr(final, "value", None)
    ids = [c.id for c in getattr(view, "choices", ())]
    out: list[Any] = [value, None]
    kind = getattr(q, "kind", None)
    if kind == "choice" and not view.multiple:
        out += ids
    elif kind == "choice":
        base = list(value) if isinstance(value, list) else []
        out += [sorted(set(base) ^ {c}) for c in ids]
    elif kind in ("slider", "constant_sum") or (kind == "text_entry" and isinstance(value, dict)):
        base = dict(value) if isinstance(value, dict) else {}
        items = list(base) or [
            c.id for c in getattr(q, "items", None) or getattr(q, "fields", None) or []
        ]
        numbers = _numbers(constants, q)
        for item in items:
            out += [{**base, item: _num(x)} for x in numbers]
            out += [{**base, item: c} for c in sorted(constants) if kind == "text_entry"]
    elif kind == "text_entry":
        out += sorted(constants) + [""]
        out += [_num(x) for x in _numbers(constants, q)]
    elif kind == "matrix" and getattr(q, "mode", None) == "single":
        base = dict(value) if isinstance(value, dict) else {}
        for row in [r.id for r in getattr(q, "rows", [])]:
            out += [{**base, row: col.id} for col in getattr(q, "columns", [])]
    encoded = list(dict.fromkeys(json.dumps(v, sort_keys=True) for v in out))
    return tuple(encoded[:MAX_ANSWERS])


def _placements(survey: Survey) -> dict[str, int]:
    """Question id -> how many times the flow places its block."""
    blocks: dict[str, int] = {}
    for node in survey.walk_flow():
        block_id = getattr(node, "block_id", None)
        if block_id:
            blocks[block_id] = blocks.get(block_id, 0) + 1
    return {
        q: n for b, n in blocks.items() if b in survey.blocks for q in survey.blocks[b].question_ids
    }


def _constants(survey: Survey) -> dict[str, set[str]]:
    """Question id -> the values the survey's logic compares its answers with."""
    out: dict[str, set[str]] = {}

    def visit(obj: Any) -> None:
        if isinstance(obj, Comparison):
            if obj.left.question_id and obj.right is not None:
                out.setdefault(obj.left.question_id, set()).add(str(obj.right))
            return
        if isinstance(obj, Expression) and obj.tree is not None:
            names = {
                n.name.split(".")[0].split("_")[0]
                for n in iter_nodes(obj.tree)
                if isinstance(n, Var)
            }
            consts = {n.value.strip("'\"") for n in iter_nodes(obj.tree) if isinstance(n, Const)}
            for name in names:
                if name in survey.questions:
                    out.setdefault(name, set()).update(consts)
            return
        if isinstance(obj, BaseModel):
            for name in type(obj).model_fields:
                if name != "extras":
                    visit(getattr(obj, name))
        elif isinstance(obj, (list, tuple)):
            for x in obj:
                visit(x)
        elif isinstance(obj, dict):
            for x in obj.values():
                visit(x)

    visit(survey)
    return out


def _loop(key: RecordKey) -> str | None:
    return (key[2] or "").rpartition("@")[0] or None


def _nth(key: RecordKey) -> int:
    return int((key[2] or "@0").rpartition("@")[2])


def _score(survey: Survey, run: RespondentRun, rec: Recording) -> tuple[int, int]:
    answered = {k for k, a in rec.answers.items() if a.value not in (None, "", [], {}) or a.text}
    displayed = set(run.state.displayed)
    misfit = len(answered - displayed) + sum(
        1 for name, value in rec.outputs.items() if (run.embedded.get(name) or "") != value
    )
    responding = {
        k
        for k in displayed
        if (q := survey.questions.get(k[0])) is not None and q.kind not in NON_RESPONSE_KINDS
    }
    return misfit, len(responding - answered)


Trial = dict[RecordKey, tuple[str, ...]]
_Walked = tuple[int, list[ChoiceRequest], list[ChoiceRequest], int, list[ChoiceRequest], Trial]


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
    constants = _constants(survey)
    placements = _placements(survey)
    cache: dict[tuple, _Walked] = {}
    budget = [max_replays]

    def run(trial: Trial) -> _Walked:
        """(misfit, undecided requests, every searched request reached in walk order,
        questions displayed but blank, unordered requests reached, the decisions made)."""
        key = tuple(sorted(trial.items()))
        if key not in cache:
            if budget[0] <= 0:
                raise _OutOfReplays
            budget[0] -= 1
            open_: list[ChoiceRequest] = []
            seen: list[ChoiceRequest] = []
            unordered: list[ChoiceRequest] = []
            orders = {**rec.orders, **{k: v for k, v in trial.items() if k[0] in _ORDER_KINDS}}
            probe = _Probe(orders, trial, kinds, open_, seen, unordered)
            sim = Simulator(survey, chooser=probe, **simulator_kw)
            answerer = _ProbeAnswerer(survey, rec, trial, open_, seen, constants, placements)
            try:
                walked = sim.respondent(
                    answerer,
                    embedded=dict(rec.embedded),
                    seed=0,
                    drawn=rec.drawn,
                )
            except ExecutionError as e:
                walked = e.run
            score = _score(survey, walked, rec) if walked is not None else (1 << 30, 1 << 30)
            decisions = {
                (e.kind, e.node_id, e.loop_id): tuple(e.shown)
                for e in (walked.audit if walked is not None else ())
                if isinstance(e, RandomizerDecision) and e.kind in _PATH_KINDS
            }
            cache[key] = (score[0] + answerer.unused(), open_, seen, score[1], unordered, decisions)
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
            misfit, undecided, *_ = run(trial)
            if not undecided:
                return trial, misfit
            req = undecided[0]
            k = _key(req)
            if req.kind == ANSWER and misfit == 0:  # nothing to explain: the recorded one
                trial[k] = candidates(req, trial)[0]
                continue
            trial[k] = min(candidates(req, trial), key=lambda o: run({**trial, k: o})[0])

    conditional: set[RecordKey] = set()

    def settle(trial: Trial, within: set[str] | None) -> tuple[Trial, list[str]]:
        """The decisions reached under ``trial`` (only those under the nodes
        ``within``, if given), and notes.

        A decision the response cannot tell apart is still kept, so the replay
        reproduces the response: the choice the search settled on (equally good, by
        the response), listed in ``conditional`` and noted, with the decisions under
        it."""
        decided: Trial = {}
        notes: list[str] = []
        skip: set[str] = set()
        fits_already = run(trial)[0] == 0
        for req in run(trial)[2]:
            k = _key(req)
            if k[1] in skip or (within is not None and k[1] not in within):
                continue
            if req.kind == ANSWER and fits_already:  # keep what the search settled on
                if k in trial:
                    decided[k] = trial[k]
                continue
            scored = [(complete({**trial, k: o}), o) for o in candidates(req, trial)]
            best = min(m for (_, m), _ in scored)
            fits = [(t, o) for (t, m), o in scored if m == best]
            if len(fits) == 1 or k[0] == ANSWER:
                # earlier answers that fit equally give the same response: keep one
                decided[k] = fits[0][1]
                continue
            # among equally good choices, the one that leaves the fewest questions
            # displayed but blank (a blank the path explains is likelier than a skip)
            chosen_t, chosen = min(fits, key=lambda f: (run(f[0])[3], f[1] != trial.get(k)))
            arms = sorted({a for _, o in fits for a in o})
            notes.append(
                f"{k[0]} {k[1]}: the response fits {', '.join(arms)} equally; kept "
                f"{'|'.join(chosen)}, which is not known to be theirs"
            )
            decided[k] = chosen
            conditional.add(k)
            below = _below(survey, k[1])
            skip |= below
            sub, sub_notes = settle(chosen_t, below)
            for key, value in sub.items():
                decided[key] = value
                conditional.add(key)  # follows a choice that is not known to be theirs
            notes += [n for n in sub_notes if n not in notes]
        return decided, notes

    notes = list(rec.unrecoverable)
    try:
        trial, misfit = complete({})
        for _ in range(3):  # improve one decision at a time until nothing changes
            changed = False
            for req in run(trial)[2]:
                k = _key(req)
                if misfit == 0 and req.kind == ANSWER:
                    continue
                scored = [(complete({**trial, k: o}), o) for o in candidates(req, trial)]
                best = min(m for (_, m), _ in scored)
                if best < misfit:
                    trial, misfit = next(x for x, _ in scored if x[1] == best)
                    changed = True
            if not changed:
                break
        # still misfitting: two decisions may only fit together (an arm of one sets
        # a field another's arm reads); try pairs jointly while that improves
        changed = misfit > 0
        while changed:
            changed = False
            reqs = [r for r in run(trial)[2] if r.kind != ANSWER]
            for r1, r2 in itertools.combinations(reqs, 2):
                k1, k2 = _key(r1), _key(r2)
                for o1 in candidates(r1, trial):
                    for o2 in candidates(r2, {**trial, k1: o1}):
                        t, m = complete({**trial, k1: o1, k2: o2})
                        if m < misfit:
                            trial, misfit, changed = t, m, True
                            break
                    if changed:
                        break
                if changed or misfit == 0:
                    break
    except _OutOfReplays:
        notes.append(f"search stopped after {max_replays} replays; nothing recovered")
        return replace(rec, unrecoverable=tuple(notes))
    try:
        decided, unsettled = settle(trial, None)
    except _OutOfReplays:
        notes.append(f"search stopped after {max_replays} replays; nothing recovered")
        return replace(rec, unrecoverable=tuple(notes))
    notes += unsettled

    def fit(t: Trial) -> tuple[int, int]:
        r = run(t)
        return r[0], r[3]

    # Orders the export does not hold (a loop's, a block's without its _DO column, a
    # randomizer's that shows every arm): improved one step at a time while the fit
    # improves. All orders are alike beforehand, so besides the misfit the search
    # also lowers the questions displayed but left blank: a blank the order explains
    # (the question was not shown) is likelier than a skip.
    found: Trial = {}
    try:
        base = {k: v for k, v in decided.items()}
        unheld: dict[RecordKey, tuple[str, ...]] = {
            k: rec.orders[k] for k in rec.orders if k[0] == "loop"
        }
        unheld.update({k: rec.orders[k] for k in rec.inferred if k[0] == "block"})
        unheld.update({_key(r): tuple(r.options) for r in run(base)[4]})
        best = fit(base)
        for k, start in unheld.items():
            if best == (0, 0):
                break
            current = list(start)
            improved = True
            while improved and best != (0, 0):
                improved = False
                for cand in _moves(current):
                    f = fit({**base, **found, k: tuple(cand)})
                    if f < best:
                        current, best, improved = cand, f, True
                        found[k] = tuple(cand)
                        break
        final = {**base, **found}
        misfit = run(final)[0]
        # every other path decision the best replay made, so a replay makes it too
        made = {
            k: v
            for k, v in run(final)[5].items()
            if k not in rec.orders and k not in final and len(v) > 1
        }
    except _OutOfReplays:
        notes.append(f"search stopped after {max_replays} replays")
        misfit, made = 0, {}
    if misfit:
        notes.append(f"the replay still differs from the response in {misfit} place(s)")
    earlier = {
        (k[1], _loop(k), _nth(k)): json.loads(v[0])
        for k, v in decided.items()
        if k[0] == ANSWER
        and json.loads(v[0]) != getattr(rec.answers.get((k[1], _loop(k))), "value", None)
    }
    flow = {k: v for k, v in decided.items() if k[0] != ANSWER}
    conditional.update(made)  # not evidence: what any equally good replay would do
    return replace(
        rec,
        orders={**made, **rec.orders, **flow, **found},
        inferred=tuple(dict.fromkeys([*rec.inferred, *flow, *found, *made])),
        unrecoverable=tuple(notes),
        conditional=tuple(dict.fromkeys([*rec.conditional, *sorted(conditional, key=str)])),
        earlier={**rec.earlier, **earlier},
    )


def _moves(order: list[str]) -> Iterator[list[str]]:
    """Orders one step from ``order``: an item moved to another place, then two items
    swapped."""
    n = len(order)
    for i in range(n):
        rest = order[:i] + order[i + 1 :]
        for j in range(n):
            if j != i:
                yield [*rest[:j], order[i], *rest[j:]]
    for i, j in itertools.combinations(range(n), 2):
        cand = list(order)
        cand[i], cand[j] = cand[j], cand[i]
        yield cand


class _OutOfReplays(Exception):
    pass


def _below(survey: Survey, node_id: str) -> set[str]:
    """Ids of the flow nodes under ``node_id``."""
    node = next((n for n in survey.walk_flow() if getattr(n, "id", None) == node_id), None)
    children = getattr(node, "children", None) or []
    return {n.id for n in walk(children) if getattr(n, "id", None)}


def _key(req: ChoiceRequest) -> RecordKey:
    return (req.kind, req.node_id, req.loop_id)
