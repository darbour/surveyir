"""Describe a survey's experimental design: what is randomized, and how.

``design(survey)`` lists every randomization point without enumerating cells
(designs combine quickly: six 1-of-3 randomizers in random order is already
3^6 x 6! paths). Each flow randomizer becomes a ``Factor`` whose arms carry
their nominal share (k/n) and the embedded-data values they assign, which is
usually how the condition is recorded in the data. Factors at the same level of
the flow are crossed; factors inside an arm are nested.

The shares are nominal: they describe the randomizer, not what a particular
respondent was exposed to. They ignore branch conditions, and under Qualtrics'
"evenly present" (least-filled) balancing the next draw depends on earlier
respondents' counts, so a given draw can be forced. ``Factor.contrast`` says
whether a factor is an exclusive exposure (k < n) or only an order contrast
(every arm shown). A researcher can declare the intended scientific contrast
with ``design(survey, annotations=...)``.

Randomization the .qsf cannot describe (JavaScript, web services, library
blocks) is listed in ``Design.opaque`` so a simulation can flag it.
"""

from __future__ import annotations

import importlib
import itertools
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

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
    nominal_share: float = Field(
        description="k/n: the share of respondents reaching the factor that the randomizer "
        "nominally assigns this arm. It is not an exposure probability, and it is not "
        "conditional on the balancing history: under even (least-filled) presentation the "
        "next draw can be forced by earlier respondents' counts."
    )
    nominal_marginal: float = Field(
        description="Product of nominal_share and the nominal shares of the enclosing arms. "
        "Branch conditions are explicitly excluded, so this is neither the chance that a "
        "respondent sees the arm nor conditional on the balancing history."
    )
    assignments: dict[str, str] = Field(
        default_factory=dict, description="Embedded data set inside this arm (field -> value)."
    )
    blocks: list[str] = Field(default_factory=list, description="Block ids inside this arm.")

    @computed_field(description="Deprecated alias of nominal_share (same value).")
    @property
    def probability(self) -> float:
        return self.nominal_share

    @computed_field(description="Deprecated alias of nominal_marginal (same value).")
    @property
    def marginal_probability(self) -> float:
        return self.nominal_marginal


class FactorAnnotation(BaseModel):
    """A researcher's declaration of the scientific contrast a factor is meant to carry."""

    model_config = ConfigDict(extra="forbid")

    contrast: Literal["exposure", "order"] | None = Field(
        default=None, description="The intended contrast: exclusive exposure or order."
    )
    treatment: str | None = Field(default=None, description="What the arms manipulate.")
    note: str | None = None


class Factor(BaseModel):
    """A flow randomizer: an exposure (k < n) or order (k = n) manipulation."""

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
        "wins, so each value is recorded with nominal share 1/n. Everyone still saw every "
        "arm; see recorded_field and contrast.",
    )
    annotation: FactorAnnotation | None = Field(
        default=None, description="The contrast a researcher declared via design(annotations=)."
    )

    @property
    def between_subjects(self) -> bool:
        """k < n, or every arm shown but a field records the last one (doublecast relies on
        this including last-shown factors). See ``contrast`` for what was compared."""
        return self.k < self.n or bool(self.last_shown_assigns)

    @computed_field(
        description="'exposure' when k < n (each respondent sees an exclusive subset of the "
        "arms); 'order' when every arm is shown, so only the order differs between respondents."
    )
    @property
    def contrast(self) -> Literal["exposure", "order"]:
        return "exposure" if self.k < self.n else "order"

    @computed_field(
        description="For last-shown factors: every respondent saw every stimulus, and these "
        "fields hold the value set by the last one shown. The contrast is order (which arm "
        "came last), not exclusive exposure. Same content as last_shown_assigns."
    )
    @property
    def recorded_field(self) -> list[str]:
        return list(self.last_shown_assigns)

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
            kind = f"{f.contrast} contrast"
            if f.recorded_field:
                kind += (
                    f"; field {', '.join(f.recorded_field)} records the last arm shown "
                    f"(each value with nominal share 1/{f.n})"
                )
            even = ", evenly presented" if f.even_presentation else ""
            where = f" within {' > '.join(f.within)}" if f.within else ""
            cond = f" if {f.condition}" if f.condition else ""
            lines.append(f"{f.id}: {kind}, {f.k} of {f.n}{even}{where}{cond}")
            if f.annotation is not None:
                lines.append(f"  declared: {_describe_annotation(f)}")
            for a in f.arms:
                assign = ", ".join(
                    f"{k}={v if len(v) <= 40 else v[:39] + '…'!r}" for k, v in a.assignments.items()
                )
                p = f"nominal share {a.nominal_share:.3g}"
                if abs(a.nominal_marginal - a.nominal_share) > 1e-9:
                    p += f", nominal marginal {a.nominal_marginal:.3g}"
                lines.append(f"  - {a.label} ({p}){': ' + assign if assign else ''}")
        for o in self.order:
            lines.append(f"{o.kind} randomized at {o.location} ({o.mode})")
        for r in self.random_values:
            lines.append(f"random value {r.field} = {r.expression}")
        for o in self.opaque:
            lines.append(f"NOT REPRODUCIBLE: {o.kind} at {o.location}: {o.detail}")
        if self.factors:
            lines.append(
                "Nominal shares are k/n, ignoring branches; not exposure probabilities, "
                "nor conditional on balancing history."
            )
        return "\n".join(lines)


def _describe_annotation(f: Factor) -> str:
    a = f.annotation
    assert a is not None
    parts = []
    if a.contrast is not None:
        parts.append(f"{a.contrast} contrast")
        if a.contrast != f.contrast:
            parts[-1] += f" (the structure gives {f.contrast})"
    if a.treatment:
        parts.append(f"treatment: {a.treatment}")
    if a.note:
        parts.append(f"note: {a.note}")
    return "; ".join(parts) or "(empty annotation)"


Annotations = Mapping[str, Mapping[str, Any] | FactorAnnotation]


def _load_annotations(annotations: Annotations | str | Path) -> dict[str, FactorAnnotation]:
    """Annotations from a mapping, or from a JSON or YAML file (YAML needs PyYAML)."""
    if isinstance(annotations, (str, Path)):
        path = Path(annotations)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            try:
                yaml = importlib.import_module("yaml")
            except ImportError as e:
                raise ImportError(
                    f"reading {path.name} needs PyYAML (pip install pyyaml), or use a .json file"
                ) from e
            raw = yaml.safe_load(text) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: annotations must be a mapping of factor id -> fields")
        annotations = raw
    return {
        str(fid): a if isinstance(a, FactorAnnotation) else FactorAnnotation.model_validate(a)
        for fid, a in annotations.items()
    }


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


def design(survey: Survey, annotations: Annotations | str | Path | None = None) -> Design:
    """The survey's randomization points.

    ``annotations`` maps factor ids to a declared contrast, e.g.
    ``{"FL_5": {"contrast": "exposure", "treatment": "loss frame", "note": "..."}}``; it may
    also be a path to a JSON or YAML file holding that mapping. Unknown factor ids raise
    ``ValueError``.
    """
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
                            nominal_share=k / n if n else 0.0,
                            nominal_marginal=reach * (k / n if n else 0.0),
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
                        arm.nominal_marginal,
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
    if annotations is not None:
        declared = _load_annotations(annotations)
        by_id = {f.id: f for f in d.factors}
        unknown = sorted(set(declared) - set(by_id))
        if unknown:
            raise ValueError(
                f"annotations name unknown factors {unknown}; factors are {sorted(by_id)}"
            )
        for fid, a in declared.items():
            by_id[fid].annotation = a
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
