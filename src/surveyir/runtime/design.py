"""Describe a survey's experimental design: what is randomized, and how.

``design(survey)`` lists every randomization point without enumerating cells
(designs combine quickly: six 1-of-3 randomizers in random order is already
3^6 x 6! paths). Each between-subjects randomizer becomes a ``Factor`` whose
arms carry their inclusion probability and the embedded-data values they
assign, which is usually how the condition is recorded in the data. Factors
at the same level of the flow are crossed; factors inside an arm are nested.

Randomization the .qsf cannot describe (JavaScript, web services, library
blocks) is listed in ``Design.opaque`` so a simulation can flag it.
"""

from __future__ import annotations

import itertools
import re
from typing import Literal

from pydantic import BaseModel, Field

from ..describe import describe_condition
from ..model import (
    BlockNode,
    BranchNode,
    EmbeddedDataNode,
    FlowNode,
    LibraryBlockNode,
    RandomizerNode,
    Survey,
    WebServiceNode,
    walk,
)

JS_RANDOM = re.compile(r"Math\.random|shuffle\s*\(|setEmbeddedData", re.I)


class Arm(BaseModel):
    key: str = Field(description="Key used in the FL_<id>_DO export columns.")
    flow_id: str
    label: str
    probability: float = Field(
        description="Chance this arm is shown, given the respondent reaches the factor (k/n)."
    )
    marginal_probability: float = Field(
        description="Chance a respondent sees this arm at all: probability times the "
        "probabilities of the enclosing arms. Branch conditions are not included."
    )
    assignments: dict[str, str] = Field(
        default_factory=dict, description="Embedded data set inside this arm (field -> value)."
    )
    blocks: list[str] = Field(default_factory=list, description="Block ids inside this arm.")


class Factor(BaseModel):
    """A flow randomizer: a between-subjects (k < n) or order (k = n) manipulation."""

    id: str
    description: str | None = None
    n: int
    k: int = Field(description="Arms shown per respondent.")
    even_presentation: bool
    arms: list[Arm]
    within: list[str] = Field(
        default_factory=list,
        description="Enclosing factor/arm path, e.g. ['FL_5:FL_7']; empty at top level.",
    )
    condition: str | None = Field(
        default=None, description="Branch conditions the factor sits under, in words."
    )

    last_shown_assigns: list[str] = Field(
        default_factory=list,
        description="Fields every arm sets when all arms are shown: the last arm shown "
        "wins, so this is effectively a 1-of-n assignment (each value with p = 1/n).",
    )

    @property
    def between_subjects(self) -> bool:
        return self.k < self.n or bool(self.last_shown_assigns)

    @property
    def treatment_fields(self) -> list[str]:
        """Embedded-data fields whose value differs between arms."""
        fields: dict[str, set[str]] = {}
        for arm in self.arms:
            for name, value in arm.assignments.items():
                fields.setdefault(name, set()).add(value)
        return [f for f, values in fields.items() if len(values) > 1 or len(self.arms) == 1]


class OrderRandomization(BaseModel):
    """Within-subject randomization of question, choice or loop order."""

    kind: Literal["block_questions", "choices", "loop"]
    location: str = Field(description="Block or question id.")
    mode: str
    subset_size: int | None = None
    even_presentation: bool = False
    flip_scale: bool = False


class RandomValue(BaseModel):
    field: str
    expression: str = Field(description="e.g. '${rand://int/1:4}'.")
    flow_id: str


class Opaque(BaseModel):
    kind: Literal["javascript", "web_service", "library_block"]
    location: str
    detail: str


class Design(BaseModel):
    factors: list[Factor] = Field(default_factory=list)
    order: list[OrderRandomization] = Field(default_factory=list)
    random_values: list[RandomValue] = Field(default_factory=list)
    opaque: list[Opaque] = Field(default_factory=list)

    @property
    def between_subjects(self) -> list[Factor]:
        return [f for f in self.factors if f.between_subjects]

    def crossed(self) -> list[list[Factor]]:
        """Between-subjects factors grouped by nesting path and branch condition.

        Factors in the same group are reached by the same respondents and are
        assigned independently, so they are fully crossed.
        """
        groups: dict[tuple[tuple[str, ...], str | None], list[Factor]] = {}
        for f in self.between_subjects:
            groups.setdefault((tuple(f.within), f.condition), []).append(f)
        return list(groups.values())

    def cells(self, limit: int = 1000) -> tuple[list[dict[str, str]], bool]:
        """Conditions of the top-level crossed between-subjects factors.

        Returns (cells, complete); ``complete`` is False when the count exceeds
        ``limit`` or the design has nested factors (their cells depend on the arm).
        Only k = 1 factors contribute a single arm per cell.
        """
        top = [f for f in self.between_subjects if not f.within and not f.condition and f.k == 1]
        nested = any(f.within or f.condition or f.k != 1 for f in self.between_subjects)
        cells: list[dict[str, str]] = []
        for combo in itertools.product(*[[(f.id, a.key) for a in f.arms] for f in top]):
            if len(cells) >= limit:
                return cells, False
            cells.append(dict(combo))
        return cells, not nested

    def summary(self) -> str:
        lines = []
        for f in self.factors:
            kind = "between-subjects" if f.between_subjects else "order"
            if f.last_shown_assigns:
                kind += f" (all shown; last arm sets {', '.join(f.last_shown_assigns)})"
            even = ", evenly presented" if f.even_presentation else ""
            where = f" within {' > '.join(f.within)}" if f.within else ""
            cond = f" if {f.condition}" if f.condition else ""
            lines.append(f"{f.id}: {kind}, {f.k} of {f.n}{even}{where}{cond}")
            for a in f.arms:
                assign = ", ".join(
                    f"{k}={v if len(v) <= 40 else v[:39] + '…'!r}" for k, v in a.assignments.items()
                )
                p = f"p={a.probability:.3g}"
                if f.last_shown_assigns:
                    p += f", assigned {1 / f.n:.3g}"
                if abs(a.marginal_probability - a.probability) > 1e-9:
                    p += f", overall {a.marginal_probability:.3g}"
                lines.append(f"  - {a.label} ({p}){': ' + assign if assign else ''}")
        for o in self.order:
            lines.append(f"{o.kind} randomized at {o.location} ({o.mode})")
        for r in self.random_values:
            lines.append(f"random value {r.field} = {r.expression}")
        for o in self.opaque:
            lines.append(f"NOT REPRODUCIBLE: {o.kind} at {o.location}: {o.detail}")
        return "\n".join(lines)


def _children(node: FlowNode) -> list[FlowNode]:
    return list(getattr(node, "children", None) or [])


def _assignments(nodes: list[FlowNode]) -> dict[str, str]:
    """Embedded data set in an arm, outside any nested randomizer or branch."""
    out: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, EmbeddedDataNode):
            for f in node.fields:
                if f.value is not None:
                    out[f.name] = f.value.plain
        elif not isinstance(node, (RandomizerNode, BranchNode)):
            out.update(_assignments(_children(node)))  # groups
    return out


def _blocks(node: FlowNode) -> list[str]:
    return [n.block_id for n in walk([node]) if isinstance(n, BlockNode)]


def design(survey: Survey) -> Design:
    d = Design()

    def visit(nodes: list[FlowNode], path: list[str], conds: list[str], reach: float = 1.0) -> None:
        for node in nodes:
            if isinstance(node, RandomizerNode):
                n = len(node.children)
                k = node.subset_size if node.subset_size and node.subset_size < n else n
                arms = []
                for child in node.children:
                    block = (
                        survey.blocks.get(child.block_id) if isinstance(child, BlockNode) else None
                    )
                    key = "".join(block.description.split()) if block else child.id
                    label = (block.description if block else child.description) or child.id
                    children = getattr(child, "children", None)
                    arms.append(
                        Arm(
                            key=key,
                            flow_id=child.id,
                            label=label,
                            probability=k / n if n else 0.0,
                            marginal_probability=reach * (k / n if n else 0.0),
                            assignments=_assignments(children if children is not None else [child]),
                            blocks=_blocks(child),
                        )
                    )
                shared = set.intersection(*(set(a.assignments) for a in arms)) if arms else set()
                last_wins = (
                    sorted(f for f in shared if len({a.assignments[f] for a in arms}) > 1)
                    if k == n and n > 1
                    else []
                )
                d.factors.append(
                    Factor(
                        last_shown_assigns=last_wins,
                        id=node.id,
                        description=node.description,
                        n=n,
                        k=k,
                        even_presentation=node.even_presentation,
                        arms=arms,
                        within=list(path),
                        condition=" AND ".join(conds) or None,
                    )
                )
                for child, arm in zip(node.children, arms, strict=True):
                    visit(
                        _children(child) if hasattr(child, "children") else [child],
                        [*path, f"{node.id}:{arm.key}"],
                        conds,
                        arm.marginal_probability,
                    )
            elif isinstance(node, BranchNode):
                cond = describe_condition(node.condition, survey)
                visit(node.children, path, [*conds, cond], reach)
            elif isinstance(node, EmbeddedDataNode):
                for f in node.fields:
                    for p in f.value.pipes if f.value else []:
                        if p.kind == "random":
                            d.random_values.append(
                                RandomValue(field=f.name, expression=p.raw, flow_id=node.id)
                            )
            elif isinstance(node, WebServiceNode):
                d.opaque.append(
                    Opaque(
                        kind="web_service",
                        location=node.id,
                        detail=f"{node.method or 'GET'} {node.url or ''}; sets "
                        f"{', '.join(node.sets_fields) or 'no fields'}",
                    )
                )
            elif isinstance(node, LibraryBlockNode):
                d.opaque.append(
                    Opaque(
                        kind="library_block",
                        location=node.id,
                        detail=f"library survey {node.reference_id}",
                    )
                )
            children = getattr(node, "children", None)
            if children and not isinstance(node, (RandomizerNode, BranchNode)):
                visit(children, path, conds, reach)

    visit(survey.flow, [], [])
    for block in survey.blocks.values():
        r = block.randomization
        if r is not None and r.mode != "none":
            d.order.append(
                OrderRandomization(
                    kind="block_questions",
                    location=block.id,
                    mode=r.mode,
                    subset_size=r.subset_size,
                    even_presentation=r.even_presentation,
                )
            )
        if block.loop is not None and block.loop.randomization is not None:
            lr = block.loop.randomization
            d.order.append(
                OrderRandomization(
                    kind="loop", location=block.id, mode=lr.mode, subset_size=lr.subset_size
                )
            )
    for q in survey.questions.values():
        r = getattr(q, "randomization", None)
        if r is not None:
            d.order.append(
                OrderRandomization(
                    kind="choices",
                    location=q.id,
                    mode=r.mode,
                    subset_size=r.subset_size,
                    even_presentation=r.even_presentation,
                    flip_scale=r.flip_scale,
                )
            )
        if q.javascript and JS_RANDOM.search(q.javascript):
            what = (
                "randomizes"
                if re.search(r"Math\.random|shuffle", q.javascript, re.I)
                else "sets embedded data"
            )
            d.opaque.append(
                Opaque(kind="javascript", location=q.id, detail=f"question JavaScript {what}")
            )
    return d
