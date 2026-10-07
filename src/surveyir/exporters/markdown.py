"""A readable Markdown rendering of a survey, for review or as LLM prompt context."""

from __future__ import annotations

from ..describe import describe_condition
from ..model import (
    AuthenticatorNode,
    BlockNode,
    BranchNode,
    ChoiceQuestion,
    ConstantSumQuestion,
    DrillDownQuestion,
    EmbeddedDataNode,
    EndSurveyNode,
    FlowNode,
    GroupNode,
    HeatMapQuestion,
    HighlightQuestion,
    HotSpotQuestion,
    MatrixQuestion,
    PageBreak,
    PickGroupRankQuestion,
    Question,
    QuestionRef,
    Randomization,
    RandomizerNode,
    RankOrderQuestion,
    SideBySideQuestion,
    SliderQuestion,
    Survey,
    TableOfContentsNode,
    TextEntryQuestion,
    TimingQuestion,
    UnsupportedNode,
    WebServiceNode,
)
from ..model.text import Text
from .base import Exporter


def _num(value: float | None) -> str:
    return "?" if value is None else f"{value:g}"


def _randomization(r: Randomization | None, what: str) -> str | None:
    if r is None or r.mode == "none":
        return "scale direction randomly flipped" if r and r.flip_scale else None
    if r.mode == "all":
        text = f"{what} shown in random order"
    elif r.mode == "subset":
        text = f"{r.subset_size} random {what} shown"
    else:
        fixed = [s for s in (r.slots or []) if s != "*"]
        text = f"{what} partially randomized ({len(r.randomized)} shuffled"
        text += f", {len(fixed)} fixed)" if fixed else ")"
    if r.even_presentation:
        text += ", evenly presented"
    if r.flip_scale:
        text += ", scale direction randomly flipped"
    return text


class MarkdownExporter(Exporter):
    """Survey as Markdown: flow outline, then every block and question in order.

    Options:
        language: render a translation (e.g. "ES") where available.
        include_logic: show display/skip/branch logic (default True).
        include_codes: show recode values and ids (default True).
        include_unused: also render blocks not reachable from the flow.
        include_hidden: include timing, meta-info and captcha questions.
    """

    name = "markdown"
    extension = ".md"
    media_type = "text/markdown"
    summary = "Readable Markdown of the flow and every question (good LLM context)."

    def __init__(
        self,
        *,
        language: str | None = None,
        include_logic: bool = True,
        include_codes: bool = True,
        include_unused: bool = False,
        include_hidden: bool = False,
    ) -> None:
        self.language = language
        self.include_logic = include_logic
        self.include_codes = include_codes
        self.include_unused = include_unused
        self.include_hidden = include_hidden

    # ---------------------------------------------------------------- helpers

    def _t(
        self, q: Question, text: Text, *, choice: str | None = None, answer: str | None = None
    ) -> str:
        if self.language and self.language in q.translations:
            tr = q.translations[self.language]
            if choice is not None and choice in tr.choices:
                return tr.choices[choice].plain
            if answer is not None and answer in tr.answers:
                return tr.answers[answer].plain
            if choice is None and answer is None and tr.text is not None:
                return tr.text.plain
        return text.plain

    def _code(self, item) -> str:
        if not self.include_codes:
            return ""
        code = item.recode if getattr(item, "recode", None) is not None else item.id
        return f" `[{code}]`"

    # ---------------------------------------------------------------- flow

    def _flow(self, nodes: list[FlowNode], survey: Survey, depth: int, out: list[str]) -> None:
        pad = "  " * depth
        for node in nodes:
            if isinstance(node, BlockNode):
                block = survey.blocks.get(node.block_id)
                name = block.description if block else node.block_id
                out.append(f"{pad}- Block: **{name}**")
            elif isinstance(node, EmbeddedDataNode):
                parts = []
                for f in node.fields:
                    if f.value is not None and f.value.plain != "":
                        parts.append(f"`{f.name}` = {f.value.plain[:60]!r}")
                    else:
                        parts.append(f"`{f.name}` ({f.source})")
                out.append(f"{pad}- Set embedded data: " + ", ".join(parts))
            elif isinstance(node, BranchNode):
                out.append(f"{pad}- If {describe_condition(node.condition, survey)}:")
                self._flow(node.children, survey, depth + 1, out)
            elif isinstance(node, RandomizerNode):
                n = len(node.children)
                k = node.subset_size if node.subset_size and node.subset_size < n else n
                even = ", evenly presented" if node.even_presentation else ""
                out.append(f"{pad}- Randomize: show {k} of {n} in random order{even}")
                self._flow(node.children, survey, depth + 1, out)
            elif isinstance(node, GroupNode):
                out.append(f"{pad}- Group{': ' + node.description if node.description else ''}")
                self._flow(node.children, survey, depth + 1, out)
            elif isinstance(node, EndSurveyNode):
                out.append(f"{pad}- End survey ({node.termination})")
            elif isinstance(node, WebServiceNode):
                out.append(f"{pad}- Web service call {node.url or ''}".rstrip())
            elif isinstance(node, (AuthenticatorNode, TableOfContentsNode)):
                out.append(f"{pad}- {node.type.replace('_', ' ').title()}")
                self._flow(node.children, survey, depth + 1, out)
            elif isinstance(node, UnsupportedNode):
                out.append(f"{pad}- Unsupported flow element `{node.source_type}`")
                self._flow(node.children, survey, depth + 1, out)

    # ---------------------------------------------------------------- questions

    def _question(self, q: Question, survey: Survey, out: list[str]) -> None:
        detail = q.kind.replace("_", " ")
        if isinstance(q, ChoiceQuestion):
            detail = f"{'multiple' if q.multiple else 'single'} choice, {q.layout}"
        elif isinstance(
            q,
            (
                MatrixQuestion,
                TextEntryQuestion,
                SliderQuestion,
                ConstantSumQuestion,
                RankOrderQuestion,
            ),
        ):
            detail = f"{detail}, {q.mode.replace('_', ' ')}"
        out.append(f"#### `{q.export_tag}` · {detail}")
        text = self._t(q, q.text)
        if text:
            out.append("")
            out.extend(f"> {line}" if line else ">" for line in text.split("\n"))
        notes = []
        if self.include_logic and q.display_logic is not None:
            notes.append(f"Shown only if {describe_condition(q.display_logic, survey)}.")
        if q.validation is not None:
            v = q.validation
            if v.required == "force":
                notes.append("Response required.")
            elif v.required == "request":
                notes.append("Response requested.")
            if v.content_type:
                notes.append(f"Must be {v.content_type.removeprefix('Valid').lower()}.")
            if v.number and (v.number.min is not None or v.number.max is not None):
                notes.append(f"Range {v.number.min}–{v.number.max}.")
            if v.min_chars:
                notes.append(f"At least {v.min_chars} characters.")
            if v.min_choices or v.max_choices:
                notes.append(f"Select {v.min_choices or 0}–{v.max_choices or 'any'}.")
            if v.total is not None:
                notes.append(f"Must total {v.total:g}.")
            if self.include_logic and v.custom is not None:
                notes.append(f"Valid only if {describe_condition(v.custom, survey)}.")
        if isinstance(q, TimingQuestion):
            if q.min_seconds:
                notes.append(f"Next button appears after {q.min_seconds:g}s.")
            if q.auto_advance_seconds:
                notes.append(f"Auto-advances after {q.auto_advance_seconds:g}s.")
        rand = getattr(q, "randomization", None)
        what = "rows" if isinstance(q, MatrixQuestion) else "choices"
        if (r := _randomization(rand, what)) is not None:
            notes.append(r[0].upper() + r[1:] + ".")
        cf = getattr(q, "carry_forward", None)
        if cf is not None:
            notes.append(f"Choices carried forward from {cf.question_id} ({cf.mode}).")
        if q.kind == "unsupported":
            notes.append(f"Unsupported source type `{q.origin.type}/{q.origin.selector}`.")
        out.append("")
        if notes:
            out.append("_" + " ".join(notes) + "_")
            out.append("")

        items = (
            getattr(q, "choices", None)
            or getattr(q, "items", None)
            or getattr(q, "fields", None)
            or getattr(q, "levels", None)
            or getattr(q, "points", None)
            or []
        )
        if isinstance(q, SideBySideQuestion):
            out.append("Rows:")
            for row in q.rows:
                out.append(f"- {self._t(q, row.text, choice=row.id)}{self._code(row)}")
            out.append("")
            for i, sub in enumerate(q.questions, start=1):
                scale = " | ".join(
                    f"{a.text.plain}{self._code(a)}" for a in getattr(sub, "columns", []) or []
                )
                mode = getattr(sub, "mode", sub.kind)
                out.append(f"Column {i} ({mode}): {sub.text.plain}")
                if scale:
                    out.append(f"  Scale: {scale}")
            out.append("")
            return
        if isinstance(q, MatrixQuestion) and q.row_columns:
            for row in q.rows:
                options = " | ".join(a.text.plain for a in q.row_columns.get(row.id, []))
                out.append(f"- {self._t(q, row.text, choice=row.id)}: {options}")
            out.append("")
            return
        if isinstance(q, PickGroupRankQuestion):
            out.append("Groups: " + " | ".join(q.groups))
            out.append("")
        if isinstance(q, HighlightQuestion):
            out.append(f"Passage: {q.passage}")
            out.append("Categories: " + " | ".join(c.text.plain for c in q.categories))
            out.append("")
            return
        if isinstance(q, (HotSpotQuestion, HeatMapQuestion)):
            regions = ", ".join(r.label or r.id or "?" for r in q.regions)
            image = q.image.id if q.image else "image"
            out.append(f"Image `{image}`; regions: {regions or '(free clicks)'}")
            if isinstance(q, HotSpotQuestion) and q.states:
                out.append("States: " + " | ".join(a.text.plain for a in q.states))
            out.append("")
            return
        if isinstance(q, DrillDownQuestion):
            out.append(
                f"{len(q.levels)} level(s); {len(q.options)} options, e.g. "
                + ", ".join(o.text.plain for o in q.options[:8])
            )
            out.append("")
            return
        if isinstance(q, MatrixQuestion):
            out.append("Rows:")
            for row in q.rows:
                out.append(f"- {self._t(q, row.text, choice=row.id)}{self._code(row)}")
            out.append("")
            out.append(
                "Scale: "
                + " | ".join(f"{self._t(q, a.text, answer=a.id)}{self._code(a)}" for a in q.columns)
            )
            out.append("")
        elif items:
            for i, c in enumerate(items, start=1):
                extra = ""
                if c.text_entry:
                    extra += " _(+ text)_"
                if c.exclusive:
                    extra += " _(exclusive)_"
                if self.include_logic and c.display_logic is not None:
                    extra += f" _(shown if {describe_condition(c.display_logic, survey)})_"
                out.append(f"{i}. {self._t(q, c.text, choice=c.id)}{self._code(c)}{extra}")
            out.append("")
        if isinstance(q, SliderQuestion) and (q.min is not None or q.max is not None):
            labels = f"; labels: {', '.join(q.labels)}" if q.labels else ""
            na = f"; plus “{q.not_applicable}”" if q.not_applicable else ""
            out.append(f"Scale {_num(q.min)} to {_num(q.max)}{labels}{na}")
            out.append("")

    # ---------------------------------------------------------------- document

    def export(self, survey: Survey) -> str:
        out: list[str] = [f"# {survey.name}", ""]
        n_q = sum(1 for _ in survey.iter_questions(responses_only=True))
        out.append(
            f"_{n_q} questions in {len(survey.blocks)} blocks · source: {survey.source.format}_"
        )
        out.append("")
        out.append("## Flow")
        out.append("")
        self._flow(survey.flow, survey, 0, out)
        out.append("")
        block_ids = survey.flow_block_ids()
        if self.include_unused:
            block_ids += survey.unused_block_ids()
        from ..runtime.design import design as experimental_design

        summary = experimental_design(survey).summary()
        if summary:
            out.append("## Experimental design")
            out.append("")
            out.extend(f"    {line}" for line in summary.splitlines())
            out.append("")
        for design in survey.conjoints:
            out.append(
                f"## Conjoint ({design.kind.replace('_', ' ')}, {design.confidence} confidence)"
            )
            out.append("")
            shape = []
            if design.tasks:
                shape.append(f"{design.tasks} tasks")
            if design.profiles:
                shape.append(f"{design.profiles} profiles per task")
            if design.field_prefix:
                shape.append(
                    f"levels stored in `{design.field_prefix}-<task>-<profile>-<attribute>`"
                )
            if shape:
                out.append("_" + "; ".join(shape) + "_")
                out.append("")
            for f in design.features:
                levels = " | ".join(f.levels) if f.levels else "(levels not in file)"
                out.append(f"- **{f.name}**: {levels}")
            out.append("")
        out.append("## Blocks")
        for bid in block_ids:
            block = survey.blocks.get(bid)
            if block is None:
                continue
            out.append("")
            out.append(f"### {block.description or bid}")
            notes = []
            if (r := _randomization(block.randomization, "questions")) is not None:
                notes.append(r)
            if block.loop is not None:
                n = len(block.loop.fields)
                src = (
                    f"once per row of a {n}-row table"
                    if n
                    else f"once per choice of {block.loop.question_id}"
                )
                if block.loop.randomization and block.loop.randomization.subset_size:
                    src += f" ({block.loop.randomization.subset_size} random rows)"
                notes.append(f"repeats {src}")
            if notes:
                out.append("")
                out.append("_" + "; ".join(notes) + "_")
            for element in block.elements:
                if isinstance(element, PageBreak):
                    out.append("")
                    out.append("---")
                    continue
                assert isinstance(element, QuestionRef)
                q = survey.questions.get(element.question_id)
                if q is None:
                    continue
                if not self.include_hidden and q.kind in ("timing", "meta_info", "captcha"):
                    continue
                out.append("")
                self._question(q, survey, out)
                if self.include_logic:
                    for skip in element.skip_logic:
                        dest = skip.target_question_id or skip.destination.replace("_", " ")
                        out.append(
                            f"_Skip to {dest} if {describe_condition(skip.condition, survey)}._"
                        )
                        out.append("")
        return "\n".join(line.rstrip() for line in out).rstrip() + "\n"
