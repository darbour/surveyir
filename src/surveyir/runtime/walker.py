"""Walk a survey as one respondent would experience it.

``Simulator`` samples respondents one at a time. Each respondent gets a seeded
random stream for every within-respondent decision (randomizer picks, question
and choice order, random embedded values). Qualtrics' "evenly present"
randomizers depend on everyone who came before, so a ``Counterbalancer`` shared
across the simulated sample assigns those least-filled first, as Qualtrics does.
With the same seed and the same counterbalancer state, a run is reproducible.

Answers come from an ``answerer`` callback, called once per displayed question
with a ``QuestionView`` (text with piped values filled in, choices in displayed
order). Display logic, skip logic, branches and carry forward then react to
those answers, so the walk and the answering are interleaved.

Randomization semantics were checked against 13,879 real respondents (see
tests/test_runtime_validation.py): a randomizer with "present k of n" shows
exactly k children in a uniformly random order; "evenly present" balances which
children are shown but not their order.
"""

from __future__ import annotations

import contextlib
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..model import (
    AuthenticatorNode,
    Block,
    BlockNode,
    BranchNode,
    Choice,
    ChoiceQuestion,
    ConstantSumQuestion,
    DrillDownQuestion,
    EmbeddedDataNode,
    EndSurveyNode,
    FileUploadQuestion,
    FlowNode,
    GraphicSliderQuestion,
    GroupNode,
    HeatMapQuestion,
    HighlightQuestion,
    HotSpotQuestion,
    LibraryBlockNode,
    MatrixQuestion,
    PageBreak,
    PickGroupRankQuestion,
    Question,
    QuestionRef,
    QuotaNode,
    Randomization,
    RandomizerNode,
    RankOrderQuestion,
    ScalePoint,
    SideBySideQuestion,
    SignatureQuestion,
    SliderQuestion,
    Survey,
    TableOfContentsNode,
    TextEntryQuestion,
    UnsupportedNode,
    UnsupportedQuestion,
    WebServiceNode,
)
from ..model.question import NON_RESPONSE_KINDS
from .logic import evaluate
from .pipes import render
from .state import Answer, LoopContext, RespondentState

# --------------------------------------------------------------------------- counterbalancing


class Counterbalancer:
    """Presentation counts shared across respondents, for "evenly present" randomizers.

    Picks the least-presented options first (ties broken at random), then counts
    the presentation. ``to_dict``/``from_dict`` let a long simulation resume.
    """

    def __init__(self, counts: dict[str, dict[str, int]] | None = None) -> None:
        self.counts: dict[str, dict[str, int]] = counts or {}

    def choose(self, key: str, options: Sequence[str], k: int, rng: random.Random) -> list[str]:
        counts = self.counts.setdefault(key, {})
        ranked = sorted(options, key=lambda o: (counts.get(o, 0), rng.random()))
        chosen = list(ranked[:k])
        for o in chosen:
            counts[o] = counts.get(o, 0) + 1
        return chosen

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {k: dict(v) for k, v in self.counts.items()}

    @classmethod
    def from_dict(cls, data: dict[str, dict[str, int]]) -> Counterbalancer:
        return cls({k: dict(v) for k, v in data.items()})


# --------------------------------------------------------------------------- views & answerers


@dataclass
class ChoiceView:
    id: str
    text: str


@dataclass
class QuestionView:
    """What the respondent sees for one question (piped text already filled in)."""

    question: Question
    block_id: str
    page: int
    text: str
    choices: list[ChoiceView]  # choices / items / rows / fields, in displayed order
    columns: list[ChoiceView] = field(default_factory=list)  # matrix scale, displayed order
    loop: LoopContext | None = None
    scale_flipped: bool = False

    @property
    def kind(self) -> str:
        return self.question.kind


class Answerer(Protocol):
    def __call__(self, view: QuestionView, state: RespondentState) -> Any: ...


def no_answer(view: QuestionView, state: RespondentState) -> Any:
    """Answer nothing (logic that depends on answers evaluates false)."""
    return None


class RandomAnswerer:
    """Uniformly random, well-formed answers; useful for exercising a design."""

    def __init__(self, seed: int | None = None, skip_rate: float = 0.0) -> None:
        self.rng = random.Random(seed)
        self.skip_rate = skip_rate

    def __call__(self, view: QuestionView, state: RespondentState) -> Any:
        q, rng = view.question, self.rng
        ids = [c.id for c in view.choices]
        cols = [c.id for c in view.columns]
        if rng.random() < self.skip_rate:
            return None
        if isinstance(q, ChoiceQuestion) and ids:
            if q.multiple:
                return rng.sample(ids, rng.randint(1, len(ids)))
            return rng.choice(ids)
        if isinstance(q, MatrixQuestion) and ids:
            if q.mode == "text":
                return {r: {c: "text" for c in cols} for r in ids}
            if q.mode == "multiple" and cols:
                return {r: rng.sample(cols, rng.randint(1, len(cols))) for r in ids}
            row_cols = {r: [a.id for a in q.row_columns.get(r, [])] or cols for r in ids}
            return {r: rng.choice(c) for r, c in row_cols.items() if c}
        if isinstance(q, TextEntryQuestion):
            return {f: "text" for f in ids} if q.mode == "form" else "text"
        if isinstance(q, SliderQuestion):
            lo, hi = q.min if q.min is not None else 0, q.max if q.max is not None else 100
            return {i: round(rng.uniform(lo, hi)) for i in ids}
        if isinstance(q, ConstantSumQuestion) and ids:
            total = (q.validation.total if q.validation and q.validation.total else 100) or 100
            cuts = sorted(rng.uniform(0, total) for _ in range(len(ids) - 1))
            parts = [b - a for a, b in zip([0, *cuts], [*cuts, total], strict=True)]
            return {i: round(p) for i, p in zip(ids, parts, strict=True)}
        if isinstance(q, RankOrderQuestion):
            order = rng.sample(ids, len(ids))
            return {i: order.index(i) + 1 for i in ids}
        if isinstance(q, GraphicSliderQuestion) and ids:
            return rng.choice(ids)
        if isinstance(q, SideBySideQuestion):
            return {
                s.id: {
                    r: rng.choice([a.id for a in getattr(s, "columns", [])] or [""]) for r in ids
                }
                for s in q.questions
            }
        return None


class ScreenerAwareAnswerer(RandomAnswerer):
    """Random answers, except free-text answers contain every string the survey's
    own logic tests that question's text for.

    Surveys often screen respondents on typed text ("end the survey unless the
    answer contains 'yes'"). Random text fails those screeners, so every simulated
    respondent would exit early. This answerer gets through them, which makes it
    the right default for exercising a design.
    """

    def __init__(self, survey: Survey, seed: int | None = None, skip_rate: float = 0.0) -> None:
        super().__init__(seed, skip_rate)
        from ..model import iter_comparisons, walk

        self.strings: dict[str, list[str]] = {}
        conditions = [getattr(n, "condition", None) for n in walk(survey.flow)]
        conditions += [q.display_logic for q in survey.questions.values()]
        for cond in conditions:
            if cond is None:
                continue
            for c in iter_comparisons(cond):
                if c.left.selector == "ChoiceTextEntryValue" and c.right and c.left.question_id:
                    self.strings.setdefault(c.left.question_id, []).append(c.right)

    def __call__(self, view: QuestionView, state: RespondentState) -> Any:
        q = view.question
        if isinstance(q, TextEntryQuestion) and q.mode != "form" and q.id in self.strings:
            return " ".join(self.strings[q.id])
        return super().__call__(view, state)


# --------------------------------------------------------------------------- run record


@dataclass
class RespondentRun:
    """One simulated respondent: the path taken and the answers given."""

    index: int
    seed: int
    state: RespondentState
    displayed: list[tuple[str, str | None]] = field(default_factory=list)
    choice_order: dict[tuple[str, str | None], list[str]] = field(default_factory=dict)
    column_order: dict[tuple[str, str | None], list[str]] = field(default_factory=dict)
    block_order: dict[tuple[str, str | None], list[str]] = field(default_factory=dict)
    flow_order: dict[str, list[str]] = field(default_factory=dict)
    loops: dict[str, list[str]] = field(default_factory=dict)
    finished: bool = True
    ended_by: str | None = None  # flow id of an End of Survey element, or "skip logic"
    response_flag: str | None = None

    @property
    def embedded(self) -> dict[str, str]:
        return self.state.embedded

    @property
    def answers(self) -> dict[tuple[str, str | None], Answer]:
        return self.state.answers

    @property
    def notes(self) -> list[str]:
        return self.state.notes

    def row(self, survey: Survey, options: Any = None) -> dict[str, Any]:
        """This respondent as {column name: value} (see ``rows.to_row``)."""
        from .rows import to_row

        return to_row(self, survey, options)

    def cells(self, survey: Survey, options: Any = None) -> list[tuple[Any, Any]]:
        """This respondent as ordered (Column, value) pairs, exactly as exported."""
        from .rows import to_cells

        return to_cells(self, survey, options)


class _EndSurvey(Exception):
    pass


# --------------------------------------------------------------------------- ordering helpers


def arrange(
    ids: Sequence[str],
    randomization: Randomization | None,
    rng: random.Random,
    *,
    balancer: Counterbalancer | None = None,
    key: str = "",
) -> list[str]:
    """Order (and possibly subset) ``ids`` as Qualtrics would for one respondent."""
    ids = list(ids)
    r = randomization
    if r is None or r.mode == "none":
        return ids
    if r.mode == "all":
        return rng.sample(ids, len(ids))
    if r.mode == "subset":
        k = min(r.subset_size or len(ids), len(ids))
        if r.even_presentation and balancer is not None:
            chosen = balancer.choose(key, ids, k, rng)
        else:
            chosen = rng.sample(ids, k)
        return rng.sample(chosen, len(chosen))
    # advanced: fixed ids stay put, "*" slots are filled from the shuffled pool
    hidden = set(r.undisplayed)
    pool = [i for i in r.randomized if i in ids and i not in hidden]
    pool = rng.sample(pool, len(pool))
    if r.subset_size:
        pool = pool[: r.subset_size]
    if not r.slots:
        positions = [n for n, i in enumerate(ids) if i in set(r.randomized)]
        out = list(ids)
        for pos, new in zip(positions, pool, strict=False):
            out[pos] = new
        return [i for i in out if i not in hidden]
    out = []
    for slot in r.slots:
        if slot == "*":
            if pool:
                out.append(pool.pop(0))
        elif slot in ids and slot not in hidden:
            out.append(slot)
    out += [i for i in ids if i not in out and i not in hidden and i not in set(r.randomized)]
    return out


# --------------------------------------------------------------------------- simulator


class Simulator:
    """Generate respondents for a survey.

    >>> sim = Simulator(survey, seed=1)
    >>> run = sim.respondent(answerer=RandomAnswerer(seed=2))
    >>> run.flow_order, run.embedded

    ``embedded`` supplies per-respondent fields Qualtrics would receive from a
    panel or URL (``Recipient`` embedded data such as PROLIFIC_PID).
    ``web_service`` lets the caller emulate a web-service element: it receives the
    node and state and returns the embedded fields to set.
    """

    def __init__(
        self,
        survey: Survey,
        *,
        seed: int | None = None,
        balancer: Counterbalancer | None = None,
        device: str = "desktop",
        location: dict[str, str] | None = None,
        web_service: Callable[[WebServiceNode, RespondentState], dict[str, str]] | None = None,
    ) -> None:
        self.survey = survey
        self.seed_rng = random.Random(seed)
        self.balancer = balancer or Counterbalancer()
        self.device = device
        self.location = dict(location or {})
        self.web_service = web_service
        self.quota_counts: dict[str, int] = {}
        self.count = 0

    # ---------------------------------------------------------------- public

    def respondent(
        self,
        answerer: Answerer = no_answer,
        *,
        embedded: dict[str, str] | None = None,
        seed: int | None = None,
    ) -> RespondentRun:
        seed = self.seed_rng.randrange(2**32) if seed is None else seed
        state = RespondentState(
            embedded=dict(embedded or {}), device=self.device, location=dict(self.location)
        )
        state.quotas_met = {
            qid
            for qid, q in ((q.id, q) for q in self.survey.quotas)
            if q.limit is not None and self.quota_counts.get(qid, 0) >= q.limit
        }
        run = RespondentRun(index=self.count, seed=seed, state=state)
        self.count += 1
        walk = _Walk(self, run, random.Random(seed), answerer)
        with contextlib.suppress(_EndSurvey):
            walk.nodes(self.survey.flow)
        for quota in self.survey.quotas:
            if quota.condition is not None and evaluate(quota.condition, state, self.survey):
                self.quota_counts[quota.id] = self.quota_counts.get(quota.id, 0) + 1
        return run

    def run(self, n: int, answerer: Answerer = no_answer, **kw: Any) -> list[RespondentRun]:
        return [self.respondent(answerer, **kw) for _ in range(n)]


class _Walk:
    def __init__(
        self, sim: Simulator, run: RespondentRun, rng: random.Random, answerer: Answerer
    ) -> None:
        self.sim, self.survey, self.run, self.rng = sim, sim.survey, run, rng
        self.state = run.state
        self.answerer = answerer
        self.flip = rng.random() < 0.5  # consistent scale reversal across questions

    # ---------------------------------------------------------------- flow

    def nodes(self, nodes: Sequence[FlowNode]) -> None:
        for node in nodes:
            self.node(node)

    def node(self, node: FlowNode) -> None:
        state = self.state
        if isinstance(node, BlockNode):
            block = self.survey.blocks.get(node.block_id)
            if block is None:
                state.note(f"flow references missing block {node.block_id}")
                return
            self.block(block)
        elif isinstance(node, EmbeddedDataNode):
            for f in node.fields:
                if f.source == "custom" and f.value is not None:
                    state.embedded[f.name] = render(f.value, state, self.survey, self.rng)
                else:
                    state.embedded.setdefault(f.name, "")
        elif isinstance(node, BranchNode):
            if node.condition is None:
                state.note(f"branch {node.id} has no readable condition; skipped")
            elif evaluate(node.condition, state, self.survey):
                self.nodes(node.children)
        elif isinstance(node, RandomizerNode):
            self.randomizer(node)
        elif isinstance(node, (GroupNode, AuthenticatorNode, TableOfContentsNode)):
            self.nodes(node.children)
        elif isinstance(node, EndSurveyNode):
            self.run.ended_by = node.id
            self.run.response_flag = node.response_flag
            self.run.finished = node.response_flag is None
            raise _EndSurvey
        elif isinstance(node, WebServiceNode):
            if self.sim.web_service is not None:
                state.embedded.update(self.sim.web_service(node, state))
            else:
                state.note(f"web service {node.id} not executed; fields left empty")
                for name in node.sets_fields:
                    state.embedded.setdefault(name, "")
        elif isinstance(node, LibraryBlockNode):
            state.note(f"library block {node.reference_id} not in file; skipped")
        elif isinstance(node, QuotaNode):
            for quota in self.survey.quotas:
                if (
                    quota.id in state.quotas_met
                    and quota.action == "EndCurrentSurvey"
                    and evaluate(quota.condition, state, self.survey)
                ):
                    self.run.ended_by = f"quota {quota.id}"
                    self.run.finished = False
                    raise _EndSurvey
        elif isinstance(node, UnsupportedNode):
            state.note(f"unsupported flow element {node.source_type} skipped")
            self.nodes(node.children)

    def randomizer(self, node: RandomizerNode) -> None:
        children = node.children
        keys = [self._child_key(c) for c in children]
        n = len(children)
        k = node.subset_size if node.subset_size and node.subset_size < n else n
        if node.even_presentation and k < n:
            chosen = self.sim.balancer.choose(node.id, keys, k, self.rng)
        else:
            chosen = self.rng.sample(keys, k)
        order = self.rng.sample(chosen, len(chosen))
        self.run.flow_order[node.id] = order
        by_key = dict(zip(keys, children, strict=True))
        for key in order:
            self.node(by_key[key])

    def _child_key(self, child: FlowNode) -> str:
        if isinstance(child, BlockNode) and child.block_id in self.survey.blocks:
            return "".join(self.survey.blocks[child.block_id].description.split())
        return child.id

    # ---------------------------------------------------------------- blocks

    def block(self, block: Block) -> None:
        loops = self._loops(block)
        if loops is None:
            self.block_once(block)
            return
        self.run.loops[block.id] = [lp.loop_id for lp in loops]
        outer = self.state.loop
        for lp in loops:
            self.state.loop = lp
            try:
                self.block_once(block)
            finally:
                self.state.loop = outer

    def _loops(self, block: Block) -> list[LoopContext] | None:
        loop = block.loop
        if loop is None:
            return None
        ids: list[str]
        if loop.source == "static" or not loop.question_id:
            ids = sorted(loop.fields, key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else 0))
        else:
            ids = self._loop_ids_from_question(loop.question_id, loop.locator or "", loop.fields)
        ids = arrange(
            ids, loop.randomization, self.rng, balancer=self.sim.balancer, key=f"loop:{block.id}"
        )
        return [
            LoopContext(loop_id=i, number=n, total=len(ids), fields=loop.fields.get(i, {}))
            for n, i in enumerate(ids, start=1)
        ]

    def _loop_ids_from_question(self, qid: str, locator: str, table: dict) -> list[str]:
        answer = self.state.answer(qid)
        value = answer.value if answer else None
        source = self.survey.questions.get(qid)
        all_ids = [c.id for c in getattr(source, "choices", None) or []]
        if "MergeOnNumericResponse" in locator:
            n = (
                int(float(value))
                if isinstance(value, (int, float, str)) and str(value).replace(".", "", 1).isdigit()
                else 0
            )
            return [str(i) for i in range(1, n + 1) if not table or str(i) in table]
        mode = locator.rsplit("/", 1)[-1].split("?")[0]
        selected = [str(v) for v in (value if isinstance(value, list) else [value]) if v]
        if mode == "SelectedChoices":
            return selected
        if mode in ("SelectedChoicesTextEntry", "EnteredChoicesTextEntry"):
            return [i for i in all_ids if answer and answer.text.get(i)]
        if mode == "UnselectedChoices":
            return [i for i in all_ids if i not in selected]
        if mode == "DisplayedChoices":
            return self.state.displayed_choices.get(self.state.key(qid), all_ids)
        return all_ids

    def block_once(self, block: Block) -> None:
        state = self.state
        loop_id = state.loop.loop_id if state.loop else None
        refs = {e.question_id: e for e in block.elements if isinstance(e, QuestionRef)}
        r = block.randomization
        if r is not None and r.mode != "none":
            order = arrange(
                list(refs), r, self.rng, balancer=self.sim.balancer, key=f"block:{block.id}"
            )
            per_page = r.per_page or None
            pages = (
                [order[i : i + per_page] for i in range(0, len(order), per_page)]
                if per_page
                else [order]
            )
        else:
            pages, current = [], []
            for e in block.elements:
                if isinstance(e, PageBreak):
                    pages.append(current)
                    current = []
                else:
                    current.append(e.question_id)
            pages.append(current)
            order = [q for p in pages for q in p]
        jump_to: str | None = None
        shown_before = set(state.displayed)
        try:
            self._pages(block, pages, refs, jump_to)
        finally:
            # Qualtrics numbers block display order among questions actually shown
            key = (block.id, loop_id)
            self.run.block_order[key] = [
                q
                for q in order
                if (q, loop_id) in state.displayed and (q, loop_id) not in shown_before
            ]

    def _pages(self, block: Block, pages: list[list[str]], refs: dict, jump_to: str | None) -> None:
        state = self.state
        for page_no, page in enumerate(p for p in pages if p):
            for qid in page:
                if jump_to is not None:
                    if qid != jump_to:
                        continue
                    jump_to = None
                q = self.survey.questions.get(qid)
                if q is None:
                    continue
                self.question(q, block, page_no)
                for skip in refs[qid].skip_logic if qid in refs else []:
                    if state.was_displayed(qid) and evaluate(skip.condition, state, self.survey):
                        if skip.destination == "end_of_survey":
                            self.run.ended_by = "skip logic"
                            raise _EndSurvey
                        if skip.destination == "end_of_block":
                            return
                        jump_to = skip.target_question_id
                        break

    # ---------------------------------------------------------------- questions

    def question(self, q: Question, block: Block, page: int) -> None:
        state = self.state
        if q.display_logic is not None and not evaluate(q.display_logic, state, self.survey):
            return
        key = state.key(q.id)
        state.displayed.add(key)
        self.run.displayed.append(key)
        if isinstance(q, UnsupportedQuestion):
            state.note(f"question {q.id} is an unsupported type; shown but not answered")
        items = self._items(q)
        shown = [
            c
            for c in items
            if c.display_logic is None or evaluate(c.display_logic, state, self.survey)
        ]
        r = getattr(q, "randomization", None)
        order = arrange(
            [c.id for c in shown], r, self.rng, balancer=self.sim.balancer, key=f"choices:{q.id}"
        )
        by_id = {c.id: c for c in shown}
        flipped = bool(r is not None and r.flip_scale and self.flip)
        columns: list[ScalePoint] = (
            list(getattr(q, "columns", None) or []) if isinstance(q, MatrixQuestion) else []
        )
        col_r = getattr(q, "column_randomization", None)
        col_ids = arrange([a.id for a in columns], col_r, self.rng)
        if flipped:
            if isinstance(q, MatrixQuestion):
                col_ids.reverse()
            else:
                order.reverse()
        state.displayed_choices[key] = order
        if items:
            self.run.choice_order[key] = order
        if columns:
            self.run.column_order[key] = col_ids
        if q.kind in NON_RESPONSE_KINDS:
            return
        col_by_id = {a.id: a for a in columns}
        view = QuestionView(
            question=q,
            block_id=block.id,
            page=page,
            text=render(q.text, state, self.survey, self.rng),
            choices=[
                ChoiceView(i, render(by_id[i].text, state, self.survey, self.rng)) for i in order
            ],
            columns=[ChoiceView(i, col_by_id[i].text.plain) for i in col_ids],
            loop=state.loop,
            scale_flipped=flipped,
        )
        value = self.answerer(view, state)
        if value is None:
            return
        state.answers[key] = value if isinstance(value, Answer) else Answer(value=value)

    def _items(self, q: Question) -> list[Choice]:
        if isinstance(
            q, (ChoiceQuestion, MatrixQuestion, SideBySideQuestion, PickGroupRankQuestion)
        ):
            base = list(
                getattr(q, "choices", None)
                or getattr(q, "rows", None)
                or getattr(q, "items", None)
                or []
            )
            cf = getattr(q, "carry_forward", None)
            if cf is not None:
                base = base + [c for c in self._carried(cf) if c.id not in {b.id for b in base}]
            return base
        if isinstance(q, (SliderQuestion, ConstantSumQuestion, RankOrderQuestion)):
            return list(q.items)
        if isinstance(q, TextEntryQuestion):
            return list(q.fields)
        if isinstance(q, GraphicSliderQuestion):
            return list(q.points)
        if isinstance(q, DrillDownQuestion):
            return list(q.levels)
        if isinstance(q, HighlightQuestion):
            return list(q.words)
        if isinstance(q, (HotSpotQuestion, HeatMapQuestion, FileUploadQuestion, SignatureQuestion)):
            return []
        return []

    def _carried(self, cf: Any) -> list[Choice]:
        """Carry-forward choices (ids prefixed with "x", as Qualtrics does)."""
        if cf.source != "question" or not cf.question_id:
            self.state.note(f"carry forward from {cf.raw} not supported")
            return []
        source = self.survey.questions.get(cf.question_id)
        items = list(
            getattr(source, "choices", None)
            or getattr(source, "items", None)
            or getattr(source, "rows", None)
            or []
        )
        answer = self.state.answer(cf.question_id)
        value = answer.value if answer else None
        selected = {str(v) for v in (value if isinstance(value, list) else [value]) if v}
        if isinstance(value, dict):
            selected = {k for k, v in value.items() if v not in (None, "", [])}
        shown = set(self.state.displayed_choices.get(self.state.key(cf.question_id), []))
        mode = cf.mode
        if mode == "SelectedChoices":
            keep = [c for c in items if c.id in selected]
        elif mode in ("UnselectedChoices", "NotSelectedChoices"):
            keep = [c for c in items if c.id not in selected]
        elif mode == "DisplayedChoices":
            keep = [c for c in items if c.id in shown]
        elif mode == "NotDisplayedChoices":
            keep = [c for c in items if c.id not in shown]
        elif mode in ("SelectedChoicesTextEntry", "EnteredChoicesTextEntry"):
            keep = [c for c in items if answer and answer.text.get(c.id)]
        else:
            keep = items
        return [c.model_copy(update={"id": f"x{c.id}", "display_logic": None}) for c in keep]
