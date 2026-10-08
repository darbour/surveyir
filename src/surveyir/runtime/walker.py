"""Walk a survey as one respondent would experience it.

``Simulator`` samples respondents one at a time. Each respondent gets a seeded
random stream for every within-respondent decision (randomizer picks, question
and choice order, random embedded values). Qualtrics' "evenly present"
randomizers depend on everyone who came before, so a ``Counterbalancer`` shared
across the simulated sample assigns those least-filled first, as Qualtrics does.
With the same seed and the same counterbalancer state, a run is reproducible.

Answers come from a *respondent*: an object with ``answer(ctx)``, called once
per displayed question with a ``ResponseContext`` that holds only what the
respondent has perceived (the question as displayed, the rest of the page, and
everything shown and answered before). Display logic, skip logic, branches and
carry forward then react to those answers, so the walk and the answering are
interleaved. Legacy ``answerer(view, state)`` callables still work; they can see
hidden state, so their runs are marked ``privileged``.

Each run records a ``trace`` (what the respondent saw, see ``runtime.trace``)
and an ``audit`` (what the runtime decided and could not reproduce).

Randomization semantics were checked against 13,879 real respondents (see
tests/test_runtime_validation.py): a randomizer with "present k of n" shows
exactly k children in a uniformly random order; "evenly present" balances which
children are shown but not their order.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import random
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar, cast

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
    SideBySideQuestion,
    SignatureQuestion,
    SliderQuestion,
    Survey,
    TableOfContentsNode,
    Text,
    TextEntryQuestion,
    UnsupportedNode,
    UnsupportedQuestion,
    WebServiceNode,
)
from .executability import _Inventory, external_fields
from .execution import (
    APPROXIMATIONS,
    ExecutionError,
    ExecutionPolicy,
    javascript_affects,
)
from .logic import evaluate
from .pipes import render, render_display
from .state import Answer, LoopContext, RespondentState
from .trace import (
    Affects,
    Approximation,
    AuditEvent,
    BranchEval,
    ChoiceHidden,
    ChoiceShown,
    Display,
    EmbeddedSet,
    End,
    Hidden,
    MediaRef,
    Observation,
    PageStart,
    PageSubmit,
    RandomizerDecision,
    Response,
    SkipTaken,
    SubQuestionShown,
)

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


# --------------------------------------------------------------------------- views & respondents


@dataclass
class ChoiceView:
    id: str
    text: str


@dataclass
class QuestionView:
    """What a legacy answerer is given for one question (piped text already filled in)."""

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


@dataclass(frozen=True)
class ResponseContext:
    """Everything a respondent has perceived when asked to answer ``view``.

    Holds observations only: no IR question, no logic, no JavaScript, and no
    embedded data except where it was piped into displayed text.
    """

    view: Display  # the question to answer
    page: tuple[Display, ...]  # everything visible on this page now
    history: tuple[Observation, ...]  # the visible trace so far, incl. text-only screens
    respondent_index: int


class Respondent(Protocol):
    """Answers questions from what was displayed (see ``ResponseContext``).

    Return ``None`` to leave the question unanswered, or a value shaped as in
    ``Answer``.
    """

    def answer(self, ctx: ResponseContext) -> Any: ...


class Answerer(Protocol):
    """Legacy answerer: sees the IR question and the full respondent state."""

    def __call__(self, view: QuestionView, state: RespondentState) -> Any: ...


def _is_respondent(obj: Any) -> bool:
    return getattr(obj, "__respondent_protocol__", None) == 2 or callable(
        getattr(obj, "answer", None)
    )


class _NoAnswer:
    """Answer nothing (logic that depends on answers evaluates false)."""

    __respondent_protocol__ = 2

    def answer(self, ctx: ResponseContext) -> Any:
        return None

    def __call__(self, view: QuestionView, state: RespondentState) -> Any:
        return None


no_answer = _NoAnswer()


class RandomAnswerer:
    """Uniformly random, well-formed answers; useful for exercising a design.

    Reads only what is displayed (``ctx.view``), and respects the validation the
    question displays (``Display.validation``): a number within the allowed range
    for numeric text entry, an e-mail address or ZIP code where one is required,
    text within the character limits, a number of selections within the allowed
    minimum and maximum, and constant-sum parts that add up to the total. Calling
    it as a legacy answerer, ``answerer(view, state)``, gives the same answers.
    """

    __respondent_protocol__ = 2

    def __init__(self, seed: int | None = None, skip_rate: float = 0.0) -> None:
        self.rng = random.Random(seed)
        self.skip_rate = skip_rate

    def answer(self, ctx: ResponseContext) -> Any:
        return self._draw(ctx.view)

    def __call__(self, view: QuestionView, state: RespondentState) -> Any:
        return self._draw(_legacy_display(view))

    def _draw(self, d: Display) -> Any:
        rng = self.rng
        ids = [c.id for c in d.choices]
        cols = [c.id for c in d.columns]
        if rng.random() < self.skip_rate:
            return None
        kind = d.kind
        v = d.validation
        if kind == "choice" and ids:
            if d.multiple:
                lo, hi = _int(v.get("min_choices")) or 1, _int(v.get("max_choices")) or len(ids)
                hi = max(1, min(hi, len(ids)))
                return rng.sample(ids, rng.randint(min(max(lo, 1), hi), hi))
            return rng.choice(ids)
        if kind == "matrix" and ids:
            if d.mode == "text":
                return {r: {c: _text_answer(v, rng) for c in cols} for r in ids}
            if d.mode == "multiple" and cols:
                return {r: rng.sample(cols, rng.randint(1, len(cols))) for r in ids}
            row_cols = {r: [a.id for a in d.row_columns.get(r, ())] or cols for r in ids}
            return {r: rng.choice(c) for r, c in row_cols.items() if c}
        if kind == "text_entry":
            if d.mode == "form":
                return {f: _text_answer(v, rng) for f in ids}
            return _text_answer(v, rng)
        if kind == "slider":
            lo, hi = d.bounds or (0, 100)
            return {i: round(rng.uniform(lo, hi)) for i in ids}
        if kind == "constant_sum" and ids:
            total = _num(v.get("total")) or 100
            cuts = sorted(rng.uniform(0, total) for _ in range(len(ids) - 1))
            parts = [b - a for a, b in zip([0, *cuts], [*cuts, total], strict=True)]
            out = {i: round(p) for i, p in zip(ids, parts, strict=True)}
            if _num(v.get("total")) is not None:  # the parts must add up exactly
                out[ids[-1]] += round(total) - sum(out.values())
            return out
        if kind == "rank_order":
            order = rng.sample(ids, len(ids))
            return {i: order.index(i) + 1 for i in ids}
        if kind == "graphic_slider" and ids:
            return rng.choice(ids)
        if kind == "side_by_side":
            return {
                s.id: {r: rng.choice([a.id for a in s.columns] or [""]) for r in ids}
                for s in d.subquestions
            }
        return None


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or x is None:
        return None
    try:
        out = float(x)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(x: Any) -> int | None:
    n = _num(x)
    return int(n) if n is not None else None


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ZIP = re.compile(r"^\d{5}(-\d{4})?$")


def _numeric(v: Mapping[str, Any]) -> bool:
    return v.get("content_type") == "ValidNumber" or any(
        _num(v.get(k)) is not None for k in ("number_min", "number_max"))


def _text_answer(v: Mapping[str, Any], rng: random.Random) -> str:
    """A text answer that satisfies the displayed validation ``v``.

    Without numeric validation no random draw is made, so questions without it
    get exactly the answers they always did.
    """
    if _numeric(v):
        lo, hi = _num(v.get("number_min")), _num(v.get("number_max"))
        if lo is None:
            lo = 0.0 if hi is None or hi >= 0 else hi - 100
        if hi is None or hi < lo:
            hi = lo + 100
        a, b = math.ceil(lo), math.floor(hi)
        if a <= b:  # whole numbers satisfy any decimal-places limit
            return str(rng.randint(a, b))
        return f"{(lo + hi) / 2:g}"
    content = v.get("content_type")
    if content == "ValidEmail":
        text = "respondent@example.com"
    elif content == "ValidZip":
        text = "12345"
    elif content == "ValidDate":
        text = "01/01/2024"
    else:
        text = "text"
    lo_chars, hi_chars = _int(v.get("min_chars")), _int(v.get("max_chars"))
    if lo_chars is not None and len(text) < lo_chars:
        text = " ".join([text] * (lo_chars // (len(text) + 1) + 1))[: max(lo_chars, len(text))]
    if hi_chars is not None and hi_chars >= 0 and len(text) > hi_chars:
        text = text[:hi_chars]
    return text


def validation_errors(d: Display, value: Any) -> list[str]:
    """How an answer to ``d`` violates the validation the question displays.

    Checks the rules ``Display.validation`` exposes: numeric content and range,
    e-mail and ZIP formats, character limits, the number of selections, and a
    constant-sum total. Unanswered questions are not checked, and neither is
    custom validation logic.
    """
    v = d.validation
    if not v or value is None:
        return []
    errors: list[str] = []

    def text(x: Any, where: str) -> None:
        if not isinstance(x, (str, int, float)) or isinstance(x, bool) or x == "":
            return
        s = str(x)
        if _numeric(v):
            n = _num(s.strip())
            lo, hi = _num(v.get("number_min")), _num(v.get("number_max"))
            if n is None:
                errors.append(f"{where}{s!r} is not a number")
            elif (lo is not None and n < lo) or (hi is not None and n > hi):
                errors.append(f"{where}{s} is outside [{lo}, {hi}]")
        content = v.get("content_type")
        if content == "ValidEmail" and not _EMAIL.match(s):
            errors.append(f"{where}{s!r} is not an e-mail address")
        if content == "ValidZip" and not _ZIP.match(s):
            errors.append(f"{where}{s!r} is not a ZIP code")
        lo_c, hi_c = _int(v.get("min_chars")), _int(v.get("max_chars"))
        if lo_c is not None and len(s) < lo_c:
            errors.append(f"{where}{len(s)} characters, fewer than {lo_c}")
        if hi_c is not None and len(s) > hi_c:
            errors.append(f"{where}{len(s)} characters, more than {hi_c}")

    if d.kind == "text_entry":
        for k, x in (value.items() if isinstance(value, dict) else [("", value)]):
            text(x, f"{k}: " if k else "")
    elif d.kind == "matrix" and isinstance(value, dict) and d.mode == "text":
        for r, cells in value.items():
            for c, x in (cells.items() if isinstance(cells, dict) else []):
                text(x, f"{r}/{c}: ")
    elif d.kind == "choice" and d.multiple and isinstance(value, (list, tuple)):
        lo, hi = _int(v.get("min_choices")), _int(v.get("max_choices"))
        if lo is not None and len(value) < lo:
            errors.append(f"{len(value)} selected, fewer than {lo}")
        if hi is not None and len(value) > hi:
            errors.append(f"{len(value)} selected, more than {hi}")
    elif d.kind == "constant_sum" and isinstance(value, dict):
        total = _num(v.get("total"))
        parts = [_num(x) for x in value.values()]
        if total is not None and all(p is not None for p in parts):
            got = sum(p for p in parts if p is not None)
            if abs(got - total) > 1e-6:
                errors.append(f"parts sum to {got:g}, not {total:g}")
    return errors


class ScreenerAwareAnswerer(RandomAnswerer):
    """Random answers, except free-text answers contain every string the survey's
    own logic tests that question's text for.

    Surveys often screen respondents on typed text ("end the survey unless the
    answer contains 'yes'"). Random text fails those screeners, so every simulated
    respondent would exit early. This answerer gets through them, which makes it
    the right default for exercising a design. A question screened on a number
    gets one of the numbers the logic tests (the first that meets its validation).
    It reads the survey's logic once,
    when constructed; while answering it sees only the displayed question.
    """

    def __init__(self, survey: Survey, seed: int | None = None, skip_rate: float = 0.0) -> None:
        super().__init__(seed, skip_rate)
        from ..model import iter_comparisons, walk

        self.strings: dict[str, list[str]] = {}
        conditions = [getattr(n, "condition", None) for n in walk(survey.flow)]
        conditions += [q.display_logic for q in survey.questions.values()]
        conditions += [q.in_page_display_logic for q in survey.questions.values()]
        for cond in conditions:
            if cond is None:
                continue
            for c in iter_comparisons(cond):
                if c.left.selector == "ChoiceTextEntryValue" and c.right and c.left.question_id:
                    self.strings.setdefault(c.left.question_id, []).append(c.right)

    def _draw(self, d: Display) -> Any:
        if d.kind == "text_entry" and d.mode != "form" and d.qid in self.strings:
            strings = self.strings[d.qid]
            if all(_num(x) is not None for x in strings):
                # a number screener ("equal to 50", "at most 17"): one valid number
                for x in strings:
                    if not validation_errors(d, x):
                        return x
                return super()._draw(d)
            return " ".join(strings)
        return super()._draw(d)


# --------------------------------------------------------------------------- presentation

_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""\b(src|alt)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _presentation(q: Question, text: Callable[[Text], str]) -> dict[str, Any]:
    """The respondent-visible shape of ``q`` beyond its text and options: ``Display``
    fields, with every displayed string produced by ``text``."""

    def shown(points: Sequence[Any]) -> tuple[ChoiceShown, ...]:
        return tuple(ChoiceShown(a.id, text(a.text)) for a in points)

    v = q.validation
    rules = {} if v is None else {
        k: getattr(v, k)
        for k in ("min_choices", "max_choices", "min_chars", "max_chars", "total", "content_type")
        if getattr(v, k) is not None
    }
    if v is not None and v.number is not None:
        rules.update({f"number_{k}": x for k, x in v.number.model_dump().items() if x is not None})
    mode = getattr(q, "mode", None)
    out: dict[str, Any] = {
        "kind": q.kind,
        "required": v.required if v is not None else "off",
        "validation": rules,
        "mode": mode if isinstance(mode, str) else None,
        "multiple": q.multiple if isinstance(q, ChoiceQuestion)
        else (q.mode == "multiple" if isinstance(q, MatrixQuestion) else None),
    }
    if isinstance(q, MatrixQuestion) and q.row_columns:
        out["row_columns"] = {r: shown(cols) for r, cols in q.row_columns.items()}
    if isinstance(q, SliderQuestion):
        out["bounds"] = (q.min if q.min is not None else 0, q.max if q.max is not None else 100)
    if isinstance(q, SideBySideQuestion):
        out["subquestions"] = tuple(
            SubQuestionShown(s.id, text(s.text), shown(getattr(s, "columns", None) or []))
            for s in q.questions
        )
    return out


def _media(q: Question, choices: Sequence[Choice], html: str) -> tuple[MediaRef, ...]:
    refs = [MediaRef("image", id=m.id, description=m.description) for m in q.media]
    image = getattr(q, "image", None)
    if image is not None:
        refs.append(MediaRef("image", id=image.id, description=image.description))
    refs += [MediaRef("image", id=c.image.id, description=c.image.description)
             for c in choices if c.image is not None]
    for tag in _IMG.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag)}
        refs.append(MediaRef("image", url=attrs.get("src"), description=attrs.get("alt") or None))
    return tuple(refs)


def _legacy_display(view: QuestionView) -> Display:
    """A ``Display`` for a legacy ``QuestionView`` (text as given, sub-texts plain)."""
    return Display(
        page=view.page,
        seq=0,
        qid=view.question.id,
        loop_id=view.loop.loop_id if view.loop else None,
        block_id=view.block_id,
        text=view.text,
        choices=tuple(ChoiceShown(c.id, c.text) for c in view.choices),
        columns=tuple(ChoiceShown(c.id, c.text) for c in view.columns),
        flipped=view.scale_flipped,
        **_presentation(view.question, lambda t: t.plain),
    )


# --------------------------------------------------------------------------- run record


@dataclass
class RespondentRun:
    """One simulated respondent: the path taken, the answers given, and the record.

    ``trace`` holds what the respondent perceived, in order; ``audit`` what the
    runtime decided and every approximation it made. ``privileged`` is True when
    the answers came from a legacy answerer that could read hidden state.
    """

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
    trace: tuple[Observation, ...] = ()
    audit: tuple[AuditEvent, ...] = ()
    privileged: bool = False

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


_Ob = TypeVar("_Ob")


def _public(ob: _Ob) -> _Ob:
    """``ob`` as a respondent may be given it: block ids are the runtime's, not shown."""
    if isinstance(ob, (Display, PageStart)) and ob.block_id:
        return cast(_Ob, dataclasses.replace(ob, block_id=""))
    return ob


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


def _nominal(ids: Sequence[str], r: Randomization | None) -> dict[str, float]:
    """Each option's nominal inclusion share under ``r`` (k/n), ignoring history."""
    n = len(ids)
    if r is None or r.mode in ("none", "all"):
        return {i: 1.0 for i in ids}
    if r.mode == "subset":
        return {i: min(r.subset_size or n, n) / n for i in ids}
    hidden, randomized = set(r.undisplayed), set(r.randomized)
    pool = [i for i in ids if i in randomized and i not in hidden]
    k = min(r.subset_size or len(pool), len(pool))
    if r.slots:
        k = min(k, r.slots.count("*"))
    return {
        i: 0.0 if i in hidden else (k / len(pool) if i in pool else float(i not in randomized))
        for i in ids
    }


def _given_history(before: Mapping[str, int], k: int) -> dict[str, float]:
    """Inclusion probabilities of a least-filled draw of ``k`` given the counts before it.

    Options below the k-th lowest count are certain, those above it excluded, and
    the remaining slots are shared equally among the options tied at it.
    """
    if k >= len(before):
        return {o: 1.0 for o in before}
    cut = sorted(before.values())[k - 1] if k else -1
    below = [o for o, c in before.items() if c < cut]
    tied = [o for o, c in before.items() if c == cut]
    share = (k - len(below)) / len(tied) if tied else 0.0
    return {o: 1.0 if c < cut else (share if c == cut else 0.0) for o, c in before.items()}


# --------------------------------------------------------------------------- simulator


class Simulator:
    """Generate respondents for a survey.

    >>> sim = Simulator(survey, seed=1)
    >>> run = sim.respondent(RandomAnswerer(seed=2))
    >>> run.flow_order, run.embedded, run.trace

    Execution is strict by default: the first thing the runtime cannot administer
    exactly that could change assignment, exposure, routing or outcome raises
    ``ExecutionError`` (see ``runtime.execution``). To get past a gap, supply an
    implementation, accept it with ``allow`` (by code, or ``"code:location"``;
    recorded as ``Allowed`` in the audit), or pass ``strict=False`` to record every
    gap and carry on. ``policy`` passes a ready-made ``ExecutionPolicy`` instead.

    ``implementations`` (keys as in ``runtime.execution``):

    * ``"web_service"``: ``fn(node, state) -> dict`` of embedded fields, or a dict
      of such functions by flow id;
    * ``"javascript"``: ``{qid: fn(state) -> dict}``, run when the question is
      displayed; the fields it returns are set;
    * ``"embedded"``: ``{field: value}`` for panel, recipient or URL fields
      (``respondent(embedded=...)`` overrides them per respondent);
    * ``"location"``: ``{name: value}`` for ``${loc://...}`` and GeoIP logic.

    ``web_service=`` and ``location=`` are shorthands for the first and last.
    """

    def __init__(
        self,
        survey: Survey,
        *,
        seed: int | None = None,
        balancer: Counterbalancer | None = None,
        device: str = "desktop",
        location: Mapping[str, str] | None = None,
        web_service: Callable[[WebServiceNode, RespondentState], dict[str, str]] | None = None,
        strict: bool = True,
        allow: Iterable[str] = (),
        implementations: Mapping[str, Any] | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        impl = dict(implementations or {})
        if web_service is not None:
            impl["web_service"] = web_service
        if location is not None:
            impl["location"] = {**(impl.get("location") or {}), **location}
        allow = {allow} if isinstance(allow, str) else set(allow)
        if policy is None:
            policy = ExecutionPolicy(strict=strict, allow=frozenset(allow), implementations=impl)
        else:
            if allow or strict is not True:
                raise ValueError("pass either policy= or strict=/allow=, not both")
            if impl:
                policy = dataclasses.replace(
                    policy, implementations={**policy.implementations, **impl})
        self.policy = policy
        self.survey = survey
        self.seed_rng = random.Random(seed)
        self.balancer = balancer or Counterbalancer()
        self.device = device
        supplied = policy.implementations.get("location")
        self.location: dict[str, str] = dict(supplied) if isinstance(supplied, Mapping) else {}
        supplied = policy.implementations.get("embedded")
        self.embedded: dict[str, str] = dict(supplied) if isinstance(supplied, Mapping) else {}
        self.quota_counts: dict[str, int] = {}
        self.count = 0
        self._external = set(external_fields(survey))
        self._field_affects = _Inventory(survey).field_affects

    @property
    def strict(self) -> bool:
        return self.policy.strict

    # ---------------------------------------------------------------- public

    def respondent(
        self,
        answerer: Respondent | Answerer = no_answer,
        *,
        embedded: dict[str, str] | None = None,
        seed: int | None = None,
    ) -> RespondentRun:
        """Walk one respondent. ``answerer`` is a ``Respondent`` (has ``answer(ctx)``)
        or a legacy ``(view, state)`` callable, which makes the run ``privileged``.

        Raises ``ExecutionError`` in a strict run that reaches a gap it may not
        pass; the partial run is on the error's ``run`` attribute.
        """
        seed = self.seed_rng.randrange(2**32) if seed is None else seed
        supplied = {**self.embedded, **(embedded or {})}
        state = RespondentState(
            embedded=supplied, device=self.device, location=dict(self.location)
        )
        state.unsupplied = self._external - set(supplied)
        self._quota_status(state)
        run = RespondentRun(index=self.count, seed=seed, state=state)
        self.count += 1
        walk = _Walk(self, run, random.Random(seed), answerer)
        try:
            with contextlib.suppress(_EndSurvey):
                walk.nodes(self.survey.flow)
                walk.end("flow_end", finished=True)
            # the response is recorded: it now counts toward every quota it qualifies
            # for, except one that screened it out as over quota
            for quota in self.survey.quotas:
                if run.ended_by == f"quota {quota.id}" or quota.condition is None:
                    continue
                if evaluate(quota.condition, state, self.survey):
                    self.quota_counts[quota.id] = self.quota_counts.get(quota.id, 0) + 1
        except ExecutionError as e:
            e.run = run
            raise
        finally:
            run.trace, run.audit = tuple(walk.trace), tuple(walk.audit)
        return run

    def run(
        self, n: int, answerer: Respondent | Answerer = no_answer, **kw: Any
    ) -> list[RespondentRun]:
        """``n`` respondents in turn. Keyword arguments go to ``respondent``.

        A fixed ``seed=`` here would give every respondent the same random stream;
        strict runs reject it (seed the ``Simulator`` instead).
        """
        if self.policy.strict and kw.get("seed") is not None:
            raise ValueError(
                "run(n, seed=...) reuses one seed for every respondent, so they are not "
                "independent draws; seed the Simulator instead (Simulator(survey, seed=...)), "
                "or pass strict=False"
            )
        return [self.respondent(answerer, **kw) for _ in range(n)]

    def _quota_status(self, state: RespondentState) -> None:
        """Which quotas are full, from the responses recorded so far."""
        state.quotas_met = {
            q.id for q in self.survey.quotas
            if q.limit is not None and self.quota_counts.get(q.id, 0) >= q.limit
        }


@dataclass
class _Item:
    """A question prepared at page load: its display, and what is recorded once shown."""

    q: Question
    key: tuple[str, str | None]
    block_page: int  # page index within the block (``QuestionView.page``)
    display: Display
    order: list[str]
    col_ids: list[str]
    has_items: bool
    audit: list[AuditEvent]
    pending: bool = False  # waiting for in-page display logic
    shown: bool = False
    asked: bool = False


class _Walk:
    def __init__(
        self,
        sim: Simulator,
        run: RespondentRun,
        rng: random.Random,
        answerer: Respondent | Answerer,
    ) -> None:
        self.sim, self.survey, self.run, self.rng = sim, sim.survey, run, rng
        self.state = run.state
        self.flip = rng.random() < 0.5  # consistent scale reversal across questions
        # displayed text draws (``rand://``) from its own stream, so rendering never
        # shifts the main stream's assignments and orders
        self.pipe_rng = random.Random(f"{run.seed}:pipes")
        self.trace: list[Observation] = []
        #: the trace as respondents are given it: no block ids (see ``_public``)
        self.public: list[Observation] = []
        self.audit: list[AuditEvent] = []
        self.page = 0  # global page number; 0 until the first page renders
        self.seq = 0
        self.page_open = False
        if _is_respondent(answerer):
            self.respondent: Respondent | None = answerer  # type: ignore[assignment]
            self.legacy: Answerer | None = None
        else:
            self.respondent, self.legacy = None, answerer  # type: ignore[assignment]
            run.privileged = True
        self.state.on_approximation = self._approximated
        for name, value in self.state.embedded.items():
            self.audit.append(EmbeddedSet(name, value, "respondent"))

    def _approximated(self, approx: Approximation) -> None:
        """The state's hook: the policy decides (raising in a strict run), then the
        approximation, and any ``Allowed``, is audited once per respondent."""
        allowed = self.sim.policy.handle(approx)
        if approx not in self.state.approximations:
            self.audit.append(approx)
        if allowed is not None and allowed not in self.audit:
            self.audit.append(allowed)

    def approximate(self, code: str, location: str, detail: str | None = None,
                    affects: Affects | None = None) -> None:
        """Record ``code`` at ``location``, with its registered ``affects`` and meaning
        unless given (``execution.APPROXIMATIONS``)."""
        default, meaning = APPROXIMATIONS[code]
        self.state.approximate(code, location, affects or default, detail or meaning)

    def set_embedded(self, name: str, value: Any, source: str) -> None:
        self.state.set_embedded(name, value)
        self.audit.append(EmbeddedSet(name, value, source))

    def observe(self, ob: Observation) -> None:
        """Append to the trace, and to what respondents are shown of it."""
        self.trace.append(ob)
        self.public.append(_public(ob))

    @property
    def loop_id(self) -> str | None:
        return self.state.loop.loop_id if self.state.loop else None

    def end(
        self,
        reason: Literal["flow_end", "end_survey", "skip_logic", "quota"],
        *,
        finished: bool,
        flow_id: str | None = None,
    ) -> None:
        self.observe(End(reason, finished, flow_id))

    # ---------------------------------------------------------------- flow

    def nodes(self, nodes: Sequence[FlowNode]) -> None:
        for node in nodes:
            self.node(node)

    def node(self, node: FlowNode) -> None:
        state = self.state
        if isinstance(node, BlockNode):
            block = self.survey.blocks.get(node.block_id)
            if block is None:
                self.approximate("flow.missing_block", node.block_id,
                                 f"flow element {node.id} references block {node.block_id}, "
                                 "which is not in the file; skipped")
                return
            self.block(block)
        elif isinstance(node, EmbeddedDataNode):
            for f in node.fields:
                if f.source == "custom" and f.value is not None:
                    value = render(f.value, state, self.survey, self.rng,  # rand:// assigns
                                   self.sim._field_affects(f.name))
                    self.set_embedded(f.name, value, node.id)
                elif f.name not in state.embedded:
                    # a panel / recipient / URL field nobody supplied: empty, and
                    # ``embedded.unset`` is recorded if logic or text reads it
                    state.embedded[f.name] = ""
        elif isinstance(node, BranchNode):
            if node.condition is None:
                self.approximate("branch.no_condition", node.id)
            else:
                result = evaluate(node.condition, state, self.survey)
                self.audit.append(BranchEval(node.id, result))
                if result:
                    self.nodes(node.children)
        elif isinstance(node, RandomizerNode):
            self.randomizer(node)
        elif isinstance(node, AuthenticatorNode):
            self.approximate("flow.authenticator", node.id)
            self.nodes(node.children)
        elif isinstance(node, (GroupNode, TableOfContentsNode)):
            self.nodes(node.children)
        elif isinstance(node, EndSurveyNode):
            self.run.ended_by = node.id
            self.run.response_flag = node.response_flag
            self.run.finished = node.response_flag is None
            self.end("end_survey", finished=self.run.finished, flow_id=node.id)
            raise _EndSurvey
        elif isinstance(node, WebServiceNode):
            service = self.sim.policy.implementation("web_service", node.id)
            if service is not None:
                for k, v in (service(node, state) or {}).items():
                    self.set_embedded(k, v, f"web_service:{node.id}")
            else:
                sets = ", ".join(node.sets_fields) or "no fields"
                self.approximate("web_service", node.id,
                                 f"web service {node.id} ({node.method or 'GET'} "
                                 f"{node.url or ''}) not called; sets {sets}, left empty")
                for name in node.sets_fields:
                    state.embedded.setdefault(name, "")
        elif isinstance(node, LibraryBlockNode):
            self.approximate("library_block", node.id,
                             f"library block {node.reference_id} is not in the file; skipped")
        elif isinstance(node, QuotaNode):
            self.sim._quota_status(state)  # quota status as of now, not at respondent start
            for quota in self.survey.quotas:
                if quota.id not in state.quotas_met:
                    continue
                if quota.action == "EndCurrentSurvey":
                    if evaluate(quota.condition, state, self.survey):
                        self.run.ended_by = f"quota {quota.id}"
                        self.run.finished = False
                        self.end("quota", finished=False, flow_id=node.id)
                        raise _EndSurvey
                elif quota.action not in (None, "ForBranching") and evaluate(
                    quota.condition, state, self.survey
                ):
                    self.approximate("quota.action_ignored", quota.id,
                                     f"quota {quota.id} is met; its action {quota.action!r} "
                                     "is not simulated")
        elif isinstance(node, UnsupportedNode):
            self.approximate("flow.unsupported", node.id,
                             f"unsupported flow element {node.source_type} skipped; "
                             "its children are run")
            self.nodes(node.children)

    def randomizer(self, node: RandomizerNode) -> None:
        children = node.children
        keys = [self._child_key(c) for c in children]
        n = len(children)
        k = node.subset_size if node.subset_size and node.subset_size < n else n
        before: dict[str, int] | None = None
        if node.even_presentation and k < n:
            counts = self.sim.balancer.counts.get(node.id, {})
            before = {o: counts.get(o, 0) for o in keys}
            chosen = self.sim.balancer.choose(node.id, keys, k, self.rng)
        else:
            chosen = self.rng.sample(keys, k)
        order = self.rng.sample(chosen, len(chosen))
        self.run.flow_order[node.id] = order
        self.audit.append(RandomizerDecision(
            node.id, "flow", tuple(keys), tuple(order),
            p_nominal={o: k / n for o in keys},
            p_given_history=_given_history(before, k) if before is not None else None,
            balancer_before=before, loop_id=self.loop_id,
        ))
        by_key = dict(zip(keys, children, strict=True))
        for key in order:
            self.node(by_key[key])

    def _child_key(self, child: FlowNode) -> str:
        if isinstance(child, BlockNode) and child.block_id in self.survey.blocks:
            return "".join(self.survey.blocks[child.block_id].description.split())
        return child.id

    def _arrange(
        self,
        kind: Literal["block", "choices", "columns", "loop"],
        node_id: str,
        ids: list[str],
        r: Randomization | None,
        key: str | None,
        *,
        reverse: bool = False,
    ) -> tuple[list[str], RandomizerDecision | None]:
        """``arrange`` (balanced under ``key``, if given), and the decision to audit."""
        before: dict[str, int] | None = None
        if key is not None and r is not None and r.mode == "subset" and r.even_presentation:
            counts = self.sim.balancer.counts.get(key, {})
            before = {i: counts.get(i, 0) for i in ids}
        balancer = self.sim.balancer if key is not None else None
        order = arrange(ids, r, self.rng, balancer=balancer, key=key or "")
        if reverse:
            order.reverse()
        if not ids or ((r is None or r.mode == "none") and not reverse):
            return order, None
        k = min(r.subset_size or len(ids), len(ids)) if r is not None else len(ids)
        return order, RandomizerDecision(
            node_id, kind, tuple(ids), tuple(order), p_nominal=_nominal(ids, r),
            p_given_history=_given_history(before, k) if before is not None else None,
            balancer_before=before, loop_id=self.loop_id,
        )

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
            if loop.source != "static":
                self.approximate("loop.unknown_mode", block.id,
                                 f"loop over a missing question in {block.id}; looping over "
                                 "its static fields")
            ids = sorted(loop.fields, key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else 0))
        else:
            ids = self._loop_ids_from_question(
                block.id, loop.question_id, loop.locator or "", loop.fields)
        ids, decision = self._arrange("loop", block.id, ids, loop.randomization, f"loop:{block.id}")
        if decision is not None:
            self.audit.append(decision)
        return [
            LoopContext(loop_id=i, number=n, total=len(ids), fields=loop.fields.get(i, {}))
            for n, i in enumerate(ids, start=1)
        ]

    def _loop_ids_from_question(
        self, block_id: str, qid: str, locator: str, table: dict
    ) -> list[str]:
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
        if mode == "AllChoices":
            return all_ids
        if mode == "DisplayedChoices":
            key = self.state.key(qid)
            if key not in self.state.displayed_choices:
                self.approximate("loop.no_displayed_choices", block_id,
                                 f"loop over displayed choices of {qid}, which was not "
                                 "shown; looping over all choices")
            return self.state.displayed_choices.get(key, all_ids)
        self.approximate("loop.unknown_mode", block_id,
                         f"loop over {locator!r} not supported; looping over all choices")
        return all_ids

    def block_once(self, block: Block) -> None:
        state = self.state
        loop_id = self.loop_id
        refs = {e.question_id: e for e in block.elements if isinstance(e, QuestionRef)}
        r = block.randomization
        if r is not None and r.mode != "none":
            order, decision = self._arrange("block", block.id, list(refs), r, f"block:{block.id}")
            if decision is not None:
                self.audit.append(decision)
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
        shown_before = set(state.displayed)
        try:
            self._pages(block, [p for p in pages if p], refs)
        finally:
            # Qualtrics numbers block display order among questions actually shown
            key = (block.id, loop_id)
            self.run.block_order[key] = [
                q
                for q in order
                if (q, loop_id) in state.displayed and (q, loop_id) not in shown_before
            ]

    def _pages(self, block: Block, pages: list[list[str]], refs: dict[str, QuestionRef]) -> None:
        """Administer the block's pages; skip logic acts when a page is submitted."""
        jump_to: str | None = None
        for page_no, page in enumerate(pages):
            if jump_to is not None:
                skipped = page if jump_to not in page else page[: page.index(jump_to)]
                self._hide(skipped, "skip")
                if jump_to not in page:
                    continue
                page, jump_to = page[len(skipped):], None
            items = self._page(block, page, page_no)
            self._submit()
            skip = self._skip(items, refs)
            if skip is None:
                continue
            source, destination, target = skip
            later = [q for p in pages[page_no + 1 :] for q in p]
            if destination == "end_of_survey":
                self._hide(later, "end")
                self.run.ended_by = "skip logic"
                self.end("skip_logic", finished=self.run.finished)
                raise _EndSurvey
            if destination == "end_of_block":
                self._hide(later, "skip")
                return
            # a target later on the submitted page has been shown already
            ahead = page[page.index(source) + 1 :]
            jump_to = None if target in ahead else target

    def _page(self, block: Block, page: list[str], page_no: int) -> list[_Item]:
        """Load, display and answer one page.

        Display logic, choice display logic, carry forward and piped text are
        evaluated against the state before the page; every visible question is
        displayed before any is answered. Questions are answered in page order, and
        after each answer in-page display logic can reveal questions not yet shown.
        """
        items = []
        for qid in page:
            q = self.survey.questions.get(qid)
            item = self._load(q, block, page_no) if q is not None else None
            if item is None:
                continue
            items.append(item)
            if not item.pending:
                self._show(item, block)
        while (item := next((i for i in items if i.shown and not i.asked), None)) is not None:
            item.asked = True
            self._answer(item, items)
            for later in items:
                if not later.shown and evaluate(later.q.in_page_display_logic, self.state,
                                                self.survey):
                    self._show(later, block, revealed=True)
        never = [i.q.id for i in items if not i.shown]
        self._hide(never, "in_page_display_logic")
        return items

    def _skip(
        self, items: list[_Item], refs: dict[str, QuestionRef]
    ) -> tuple[str, str, str | None] | None:
        """The first skip rule that holds at submit, in page order: (source, destination,
        target question)."""
        for item in items:
            ref = refs.get(item.q.id)
            if not item.shown or ref is None:
                continue
            for skip in ref.skip_logic:
                if evaluate(skip.condition, self.state, self.survey):
                    target = skip.target_question_id
                    self.audit.append(SkipTaken(item.q.id, self.loop_id, target or
                                                skip.destination))
                    return item.q.id, skip.destination, target
        return None

    def _hide(self, qids: Sequence[str], reason: Literal["skip", "end", "in_page_display_logic"]
              ) -> None:
        self.audit += [Hidden(q, self.loop_id, reason) for q in qids]

    def _submit(self) -> None:
        if self.page_open:
            self.page_open = False
            self.observe(PageSubmit(self.page))

    # ---------------------------------------------------------------- questions

    def _load(self, q: Question, block: Block, block_page: int) -> _Item | None:
        """Prepare ``q`` at page load; None (and audited) if display logic hides it."""
        state = self.state
        if not evaluate(q.display_logic, state, self.survey):
            self.audit.append(Hidden(q.id, self.loop_id, "display_logic"))
            return None
        # in-page logic shows the question at load if it already holds, else maybe later
        pending = not evaluate(q.in_page_display_logic, state, self.survey)
        key = state.key(q.id)
        audit: list[AuditEvent] = []
        items, carried = self._items(q)
        shown = []
        for c in items:
            if c.display_logic is None or evaluate(c.display_logic, state, self.survey):
                shown.append(c)
            else:
                audit.append(ChoiceHidden(q.id, key[1], c.id))
        r = getattr(q, "randomization", None)
        flipped = bool(r is not None and r.flip_scale and self.flip)
        matrix = isinstance(q, MatrixQuestion)
        order, decision = self._arrange(
            "choices", q.id, [c.id for c in shown], r, f"choices:{q.id}",
            reverse=flipped and not matrix,
        )
        columns = list(q.columns) if isinstance(q, MatrixQuestion) else []
        col_r = getattr(q, "column_randomization", None)
        col_ids, col_decision = self._arrange(  # evenly presented subsets are balanced
            "columns", q.id, [a.id for a in columns], col_r, f"columns:{q.id}",
            reverse=flipped and matrix,
        )
        audit += [d for d in (decision, col_decision) if d is not None]

        def text(t: Text) -> str:
            return render(t, state, self.survey, self.pipe_rng)

        plain, html = render_display(q.text, state, self.survey, self.pipe_rng)
        by_id = {c.id: c for c in shown}
        col_by_id = {a.id: a for a in columns}
        display = Display(
            page=0,
            seq=0,
            qid=q.id,
            loop_id=key[1],
            block_id=block.id,
            text=plain,
            html=html,
            choices=tuple(ChoiceShown(i, text(by_id[i].text), carried.get(i)) for i in order),
            columns=tuple(ChoiceShown(i, text(col_by_id[i].text)) for i in col_ids),
            media=_media(q, [by_id[i] for i in order], html),
            flipped=flipped,
            **_presentation(q, text),
        )
        return _Item(q, key, block_page, display, order, col_ids, bool(items), audit, pending)

    def _show(self, item: _Item, block: Block, *, revealed: bool = False) -> None:
        """Put a prepared question on the respondent's screen, and record it."""
        state, key, q = self.state, item.key, item.q
        if not self.page_open:
            self.page += 1
            self.page_open = True
            loop = state.loop
            self.observe(PageStart(
                self.page, block.id, loop.loop_id if loop else None,
                loop.number if loop else None, loop.total if loop else None,
            ))
        self.seq += 1
        item.display = dataclasses.replace(
            item.display, page=self.page, seq=self.seq, revealed_in_page=revealed
        )
        item.shown = True
        state.displayed.add(key)
        self.run.displayed.append(key)
        state.displayed_choices[key] = item.order
        if item.has_items:
            self.run.choice_order[key] = item.order
        if isinstance(q, MatrixQuestion) and q.columns:
            self.run.column_order[key] = item.col_ids
        self.observe(item.display)
        self.audit += item.audit
        if isinstance(q, UnsupportedQuestion):
            self.approximate("question.unsupported", q.id,
                             f"question {q.id} is an unsupported type; shown but not answered")
        script = self.sim.policy.implementation("javascript", q.id)
        if script is not None:  # the caller's implementation runs as the question displays
            for name, value in (script(state) or {}).items():
                self.set_embedded(name, value, f"javascript:{q.id}")
        elif (affects := javascript_affects(q.javascript)) is not None:
            what = ("randomizes or sets embedded data" if affects == "assignment"
                    else "may change what is shown")
            self.approximate("javascript", q.id,
                             f"question {q.id} has JavaScript, which is not run (it {what})",
                             affects)

    def _answer(self, item: _Item, page: Sequence[_Item]) -> None:
        """Ask the respondent for ``item`` (never for text-only screens), and record it."""
        d, state = item.display, self.state
        if not d.responds:
            return
        if self.legacy is not None:
            view = QuestionView(
                question=item.q,
                block_id=d.block_id,
                page=item.block_page,
                text=d.text,
                choices=[ChoiceView(c.id, c.text) for c in d.choices],
                columns=[ChoiceView(c.id, c.text) for c in d.columns],
                loop=state.loop,
                scale_flipped=d.flipped,
            )
            value = self.legacy(view, state)
        else:
            assert self.respondent is not None
            ctx = ResponseContext(  # what the respondent perceived: no block ids
                view=_public(d),
                page=tuple(_public(i.display) for i in page if i.shown),
                history=tuple(self.public),
                respondent_index=self.run.index,
            )
            value = self.respondent.answer(ctx)
        if value is None:
            return
        answer = value if isinstance(value, Answer) else Answer(value=value)
        errors = validation_errors(d, answer.value)
        if errors:
            self.approximate("answer.invalid", d.qid,
                             f"answer to {d.qid} violates its validation ({'; '.join(errors)}); "
                             "Qualtrics would not have accepted it")
        state.answers[item.key] = answer
        self.observe(Response(d.page, d.seq, d.qid, d.loop_id, answer.value,
                              dict(answer.text)))

    def _items(self, q: Question) -> tuple[list[Choice], dict[str, tuple[str, str]]]:
        """Options in source order, and {carried choice id: (source qid, source id)}."""
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
                ids = {b.id for b in base}
                extra = [c for c in self._carried(q, cf) if c.id not in ids]
                return base + extra, {c.id: (cf.question_id, c.id[1:]) for c in extra}
            return base, {}
        if isinstance(q, (SliderQuestion, ConstantSumQuestion, RankOrderQuestion)):
            return list(q.items), {}
        if isinstance(q, TextEntryQuestion):
            return list(q.fields), {}
        if isinstance(q, GraphicSliderQuestion):
            return list(q.points), {}
        if isinstance(q, DrillDownQuestion):
            return list(q.levels), {}
        if isinstance(q, HighlightQuestion):
            return list(q.words), {}
        if isinstance(q, (HotSpotQuestion, HeatMapQuestion, FileUploadQuestion, SignatureQuestion)):
            return [], {}
        return [], {}

    def _carried(self, q: Question, cf: Any) -> list[Choice]:
        """Carry-forward choices (ids prefixed with "x", as Qualtrics does)."""
        state = self.state
        if cf.source == "reference_list":
            self.approximate("carry_forward.reference", q.id,
                             f"{q.id} carries forward from reference list "
                             f"{cf.list_id or cf.raw}; nothing carried")
            return []
        if cf.source != "question" or not cf.question_id:
            self.approximate("carry_forward.unsupported_mode", q.id,
                             f"{q.id} carries forward from {cf.raw}; nothing carried")
            return []
        source = self.survey.questions.get(cf.question_id)
        items = list(
            getattr(source, "choices", None)
            or getattr(source, "items", None)
            or getattr(source, "rows", None)
            or []
        )
        answer = state.answer(cf.question_id)
        value = answer.value if answer else None
        selected = {str(v) for v in (value if isinstance(value, list) else [value]) if v}
        if isinstance(value, dict):
            selected = {k for k, v in value.items() if v not in (None, "", [])}
        shown = set(state.displayed_choices.get(state.key(cf.question_id), []))
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
        elif mode == "AllChoices":
            keep = items
        else:
            self.approximate("carry_forward.unsupported_mode", q.id,
                             f"{q.id} carries forward {mode!r} from {cf.question_id}, which is "
                             "not supported; carrying all choices")
            keep = items
        return [c.model_copy(update={"id": f"x{c.id}", "display_logic": None}) for c in keep]
