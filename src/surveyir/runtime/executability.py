"""Can this survey be administered exactly? A static inventory, before any run.

``executability(survey)`` walks the instrument (no rng, no answers) and lists
every feature that matters for administration with its status:

* ``executable``: the runtime administers it as Qualtrics would;
* ``needs_implementation``: it depends on something outside the file (a web
  service, question JavaScript, panel fields, a respondent's location) that the
  caller can supply through ``implementations=``;
* ``approximated``: the runtime cannot reproduce it and does something simpler;
* ``preserved_only``: kept in the IR, never executed, and changes nothing about
  administration (translations, scoring categories, conjoint metadata).

Rows that need an implementation or are approximated carry the ``Approximation``
code the runtime records when it meets them (``execution.APPROXIMATIONS``), so
``report.blocking(policy)`` is exactly what a strict run under ``policy`` would
stop on, as far as it can be known without answers.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from ..model import (
    AuthenticatorNode,
    BlockNode,
    BranchNode,
    Comparison,
    Condition,
    EmbeddedDataNode,
    EndSurveyNode,
    FlowNode,
    LibraryBlockNode,
    Pipe,
    QuestionRef,
    QuotaNode,
    RandomizerNode,
    Survey,
    TableOfContentsNode,
    Text,
    UnsupportedNode,
    UnsupportedQuestion,
    WebServiceNode,
    iter_comparisons,
)
from .execution import (
    APPROXIMATIONS,
    CARRY_FORWARD_MODES,
    IMPLEMENTED_BY,
    LOOP_MODES,
    ExecutionPolicy,
    javascript_affects,
)
from .logic import _num, operand_location, unevaluable
from .trace import EXECUTION_AFFECTING, Affects, Approximation

Status = Literal["executable", "needs_implementation", "approximated", "preserved_only"]
STATUSES: tuple[Status, ...] = (
    "needs_implementation", "approximated", "preserved_only", "executable"
)
_ORDERING = ("greater_than", "greater_than_or_equal", "less_than", "less_than_or_equal")
_RAND_INT = re.compile(r"int/(-?\d+):(-?\d+)")


@dataclass(frozen=True)
class Feature:
    """One administration-relevant feature of the instrument."""

    feature: str  # e.g. "randomizer", "question_javascript", "condition"
    location: str  # the approximation location when ``code`` is set (see execution.py)
    status: Status
    affects: Affects
    code: str | None = None
    detail: str = ""
    #: (implementations key, location) that would remove the gap
    implemented_by: tuple[str, str] | None = None

    @property
    def approximation(self) -> Approximation | None:
        if self.code is None:
            return None
        return Approximation(self.code, self.location, self.affects, self.detail)

    def resolution(self) -> str:
        """How to get past this feature in a strict run."""
        opts = []
        if self.implemented_by:
            key, loc = self.implemented_by
            opts.append(f"implementations={{{key!r}: {{{loc!r}: ...}}}}")
        opts += [f"allow {self.code}:{self.location}", f"allow {self.code}"]
        return " or ".join(opts)


@dataclass
class ExecutabilityReport:
    survey: str
    features: list[Feature] = field(default_factory=list)

    def by_status(self, status: Status) -> list[Feature]:
        return [f for f in self.features if f.status == status]

    def counts(self) -> dict[str, int]:
        c = Counter(f.status for f in self.features)
        return {s: c[s] for s in STATUSES}

    def blocking(self, policy: ExecutionPolicy) -> list[Feature]:
        """Execution-affecting gaps a run under ``policy`` would stop on.

        Empty for a permissive policy; otherwise every needs-implementation or
        approximated row that affects execution and is neither implemented nor
        allowed.
        """
        out = []
        for f in self.features:
            approx = f.approximation
            if approx is None or f.affects not in EXECUTION_AFFECTING or policy.permits(approx):
                continue
            if f.implemented_by and policy.implementation(*f.implemented_by) is not None:
                continue
            out.append(f)
        return out

    def summary(self, limit: int = 8) -> str:
        counts = self.counts()
        lines = [
            f"{self.survey}: "
            + ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in counts.items() if n)
        ]
        for status in STATUSES[:3]:
            rows = self.by_status(status)
            if not rows:
                continue
            lines.append(f"\n{status.replace('_', ' ').capitalize()}:")
            for f in rows:
                code = f" [{f.code}]" if f.code else ""
                lines.append(f"  {f.feature} at {f.location} ({f.affects}){code}: {f.detail}")
        groups: dict[str, list[Feature]] = {}
        for f in self.by_status("executable"):
            groups.setdefault(f.feature, []).append(f)
        if groups:
            lines.append("\nExecutable:")
            for name, rows in groups.items():
                locs = list(dict.fromkeys(f.location for f in rows))
                more = f", ... (+{len(locs) - limit})" if len(locs) > limit else ""
                lines.append(f"  {name} x{len(rows)}: {', '.join(locs[:limit])}{more}")
        return "\n".join(lines)

    def to_dict(self, policy: ExecutionPolicy | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "survey": self.survey,
            "counts": self.counts(),
            "features": [_row(f) for f in self.features],
        }
        if policy is not None:
            out["blocking"] = [_row(f) for f in self.blocking(policy)]
        return out


def _row(f: Feature) -> dict[str, Any]:
    d = asdict(f)
    d["implemented_by"] = list(f.implemented_by) if f.implemented_by else None
    return d


# --------------------------------------------------------------------------- inventory


def _gap(feature: str, code: str, location: str, detail: str | None = None,
         affects: Affects | None = None, status: Status | None = None) -> Feature:
    default, meaning = APPROXIMATIONS[code]
    key = IMPLEMENTED_BY.get(code)
    return Feature(
        feature=feature,
        location=location,
        status=status or ("needs_implementation" if key else "approximated"),
        affects=affects or default,
        code=code,
        detail=detail or meaning,
        implemented_by=(key, location.removeprefix("loc://")) if key else None,
    )


def _texts(obj: Any) -> Iterator[Text]:
    """Every ``Text`` in an IR subtree, except translations (never administered)."""
    if isinstance(obj, Text):
        yield obj
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            if name not in ("translations", "extras"):
                yield from _texts(getattr(obj, name))
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            yield from _texts(x)
    elif isinstance(obj, dict):
        for x in obj.values():
            yield from _texts(x)


class _Inventory:
    def __init__(self, survey: Survey) -> None:
        self.survey = survey
        self.rows: list[Feature] = []
        used = survey.flow_block_ids()  # blocks the flow can reach, in flow order
        self.blocks = [survey.blocks[b] for b in used if b in survey.blocks]
        self.questions = list({q.id: q for b in used for q in survey.block_questions(b)}.values())
        self.logic_reads: set[str] = set()  # embedded fields read by conditions
        self.pipe_reads: set[str] = set()  # embedded fields piped into text
        for cond, _, _ in self.conditions():
            for c in iter_comparisons(cond):
                if c.left.kind == "embedded_data" and c.left.name:
                    self.logic_reads.add(c.left.name)
        for _, pipe in self.pipes():
            if pipe.kind == "embedded_data" and pipe.name:
                self.pipe_reads.add(pipe.name)

    def add(self, feature: Feature) -> None:
        self.rows.append(feature)

    def field_affects(self, name: str) -> Affects:
        if name in self.logic_reads:
            return "routing"
        return "exposure" if name in self.pipe_reads else "none"

    # ---------------------------------------------------------------- sources

    def conditions(self) -> Iterator[tuple[Condition, str, str]]:
        """(condition, feature, where) for every condition the runtime evaluates."""
        for node in self.survey.walk_flow():
            if isinstance(node, BranchNode) and node.condition is not None:
                yield node.condition, "branch", node.id
        for quota in self.survey.quotas:
            if quota.condition is not None:
                yield quota.condition, "quota", quota.id
        for q in self.questions:
            if q.display_logic is not None:
                yield q.display_logic, "display_logic", q.id
            if q.in_page_display_logic is not None:
                yield q.in_page_display_logic, "in_page_display_logic", q.id
            for attr in ("choices", "rows", "items"):
                for c in getattr(q, attr, None) or []:
                    if getattr(c, "display_logic", None) is not None:
                        yield c.display_logic, "choice_display_logic", f"{q.id}/{c.id}"
        for block in self.blocks:
            for e in block.elements:
                if isinstance(e, QuestionRef):
                    for skip in e.skip_logic:
                        yield skip.condition, "skip_logic", e.question_id

    def pipes(self) -> Iterator[tuple[str, Pipe]]:
        """(where, pipe) for every reference the runtime renders."""
        for node in self.survey.walk_flow():
            if isinstance(node, EmbeddedDataNode):
                for f in node.fields:
                    if f.source == "custom" and f.value is not None:
                        for p in f.value.pipes:
                            yield f"embedded:{f.name}", p
        for q in self.questions:
            for text in _texts(q):
                for p in text.pipes:
                    yield q.id, p

    # ---------------------------------------------------------------- flow

    def flow(self, nodes: list[FlowNode]) -> None:
        for node in nodes:
            self.node(node)
            children = getattr(node, "children", None)
            if children:
                self.flow(children)

    def node(self, node: FlowNode) -> None:
        if isinstance(node, BlockNode) and node.block_id not in self.survey.blocks:
            self.add(_gap("missing_block", "flow.missing_block", node.block_id,
                          f"flow element {node.id} references block {node.block_id}"))
        elif isinstance(node, RandomizerNode):
            n = len(node.children)
            k = node.subset_size if node.subset_size and node.subset_size < n else n
            even = (", evenly presented (balanced across the simulated sample)"
                    if node.even_presentation and k < n else "")
            self.add(Feature("randomizer", node.id, "executable",
                             "assignment" if k < n else "exposure",
                             detail=f"{k} of {n}{even}"))
        elif isinstance(node, BranchNode):
            if node.condition is None:
                self.add(_gap("branch", "branch.no_condition", node.id))
            else:
                self.add(Feature("branch", node.id, "executable", "routing"))
        elif isinstance(node, WebServiceNode):
            sets = ", ".join(node.sets_fields) or "no fields"
            self.add(_gap("web_service", "web_service", node.id,
                          f"{node.method or 'GET'} {node.url or ''}; sets {sets}"))
        elif isinstance(node, LibraryBlockNode):
            self.add(_gap("library_block", "library_block", node.id,
                          f"library block {node.reference_id} is not in the file",
                          status="needs_implementation"))
        elif isinstance(node, AuthenticatorNode):
            self.add(_gap("authenticator", "flow.authenticator", node.id))
        elif isinstance(node, UnsupportedNode):
            self.add(_gap("unsupported_flow", "flow.unsupported", node.id,
                          f"unsupported flow element {node.source_type}"))
        elif isinstance(node, EndSurveyNode):
            self.add(Feature("end_survey", node.id, "executable", "routing",
                             detail=node.response_flag or node.termination))
        elif isinstance(node, QuotaNode):
            self.add(Feature("quota_check", node.id, "executable", "routing"))
        elif isinstance(node, TableOfContentsNode):
            self.add(Feature("table_of_contents", node.id, "executable", "routing",
                             detail="blocks are walked in flow order"))
        elif isinstance(node, EmbeddedDataNode):
            for f in node.fields:
                if any(p.kind == "random" for p in (f.value.pipes if f.value else [])):
                    self.add(Feature("random_value", f.name, "executable", "assignment",
                                     detail=f"{f.value.plain if f.value else ''} in {node.id}"))

    def quotas(self) -> None:
        for quota in self.survey.quotas:
            if quota.action in (None, "EndCurrentSurvey", "ForBranching"):
                self.add(Feature("quota", quota.id, "executable", "routing",
                                 detail=f"{quota.name}: {quota.action or 'no action'}"))
            else:
                self.add(_gap("quota", "quota.action_ignored", quota.id,
                              f"{quota.name}: action {quota.action} not executed"))

    def logic(self) -> None:
        for cond, feature, where in self.conditions():
            if feature in ("display_logic", "in_page_display_logic", "skip_logic",
                           "choice_display_logic"):
                self.add(Feature(feature, where, "executable",
                                 "routing" if feature == "skip_logic" else "exposure"))
            for c in iter_comparisons(cond):
                gap = self.comparison(c)
                if gap is not None:
                    code, detail = gap
                    self.add(_gap("condition", code, operand_location(c.left),
                                  f"{detail} (in {feature} {where})"))

    @staticmethod
    def comparison(c: Comparison) -> tuple[str, str] | None:
        reason = unevaluable(c)
        if reason is not None:
            return reason
        if c.operator in _ORDERING and _num(c.right) is None:
            return "logic.non_numeric", f"{c.operator} against {c.right!r}, not a number"
        if c.operator == "matches_regex":
            try:
                re.compile(c.right or "")
            except re.error as e:
                return "logic.regex_invalid", f"invalid regular expression {c.right!r} ({e})"
        return None

    # ---------------------------------------------------------------- data

    def embedded(self) -> None:
        """Panel / recipient / URL fields that logic or text read."""
        set_elsewhere = {
            name for node in self.survey.walk_flow() if isinstance(node, WebServiceNode)
            for name in node.sets_fields
        }
        scripts = " ".join(q.javascript or "" for q in self.questions)
        seen: set[str] = set()
        for node in self.survey.walk_flow():
            if not isinstance(node, EmbeddedDataNode):
                continue
            for f in node.fields:
                external = f.source != "custom" or f.value is None
                if not external or f.name in seen or f.name in set_elsewhere:
                    continue
                if f"'{f.name}'" in scripts or f'"{f.name}"' in scripts:
                    continue  # a placeholder for question JavaScript (see its row)
                affects = self.field_affects(f.name)
                if affects == "none":
                    continue
                seen.add(f.name)
                how = "read by logic" if affects == "routing" else "piped into text"
                self.add(_gap("embedded_field", "embedded.unset", f.name,
                              f"{f.source} field declared in {node.id}, {how}", affects=affects))

    def pipe_rows(self) -> None:
        for where, p in self.pipes():
            field_name = where.removeprefix("embedded:") if where.startswith("embedded:") else None
            affects: Affects = self.field_affects(field_name) if field_name else "exposure"
            if p.kind == "random":
                if _RAND_INT.match(p.selector or ""):
                    self.add(Feature("random_value", where, "executable",
                                     "assignment" if field_name else "exposure", detail=p.raw))
                else:
                    self.add(_gap("pipe", "pipe.random_unsupported", p.path,
                                  f"{p.raw} in {where}", affects=affects))
            elif p.scheme == "loc":
                self.add(_gap("pipe", "pipe.loc", p.path, f"{p.raw} in {where}", affects=affects))
            elif p.kind == "date":
                self.add(_gap("pipe", "pipe.date", p.path, f"{p.raw} in {where}", affects=affects))
            elif p.kind in ("embedded_data", "loop_merge") or (
                p.kind == "question" and p.question_id
            ):
                continue
            else:
                self.add(_gap("pipe", "pipe.unsupported", p.path, f"{p.raw} in {where}",
                              affects=affects))

    # ---------------------------------------------------------------- questions

    def question_rows(self) -> None:
        for q in self.questions:
            if isinstance(q, UnsupportedQuestion):
                self.add(_gap("unsupported_question", "question.unsupported", q.id,
                              f"{q.origin.type} question is shown but not answered"))
            js = javascript_affects(q.javascript)
            if js is not None:
                what = ("randomizes or sets embedded data" if js == "assignment"
                        else "may change what is shown")
                self.add(_gap("question_javascript", "javascript", q.id,
                              f"question JavaScript {what}", affects=js))
            cf = getattr(q, "carry_forward", None)
            if cf is not None:
                if cf.source == "reference_list":
                    self.add(_gap("carry_forward", "carry_forward.reference", q.id,
                                  f"carry forward from reference list {cf.list_id or cf.raw}"))
                elif cf.source != "question" or not cf.question_id \
                        or cf.mode not in CARRY_FORWARD_MODES:
                    self.add(_gap("carry_forward", "carry_forward.unsupported_mode", q.id,
                                  f"carry forward {cf.mode} from {cf.raw}"))
                else:
                    self.add(Feature("carry_forward", q.id, "executable", "exposure",
                                     detail=f"{cf.mode} from {cf.question_id}"))
            r = getattr(q, "randomization", None)
            if r is not None and r.mode != "none":
                self.add(Feature("choice_order", q.id, "executable", "exposure", detail=r.mode))
            cr = getattr(q, "column_randomization", None)
            if cr is not None and cr.mode != "none":
                if cr.mode == "subset" and cr.even_presentation:
                    self.add(_gap("column_order", "columns.unbalanced", q.id))
                else:
                    self.add(Feature("column_order", q.id, "executable", "exposure",
                                     detail=cr.mode))

    def block_rows(self) -> None:
        for block in self.blocks:
            r = block.randomization
            if r is not None and r.mode != "none":
                self.add(Feature("block_order", block.id, "executable", "exposure",
                                 detail=r.mode))
            loop = block.loop
            if loop is None:
                continue
            if loop.source == "static":
                self.add(Feature("loop", block.id, "executable", "exposure",
                                 detail=f"{len(loop.fields)} static loops"))
            else:
                mode = (loop.locator or "").rsplit("/", 1)[-1].split("?")[0]
                if "MergeOnNumericResponse" in (loop.locator or ""):
                    mode = "MergeOnNumericResponse"
                if not loop.question_id or mode not in LOOP_MODES:
                    self.add(_gap("loop", "loop.unknown_mode", block.id,
                                  f"loop over {loop.question_id or 'a missing question'} "
                                  f"with mode {mode or 'unknown'!r}"))
                else:
                    self.add(Feature("loop", block.id, "executable", "exposure",
                                     detail=f"{mode} of {loop.question_id}"))
            if loop.randomization is not None and loop.randomization.mode != "none":
                self.add(Feature("loop_order", block.id, "executable", "exposure",
                                 detail=loop.randomization.mode))

    def preserved(self) -> None:
        s = self.survey
        if s.languages:
            self.add(Feature("translations", "survey", "preserved_only", "none",
                             detail=", ".join(s.languages)))
        scored = [q.id for q in self.questions if q.scoring]
        if s.scoring_categories or scored:
            self.add(Feature("scoring", "survey", "preserved_only", "none",
                             detail=f"{len(s.scoring_categories)} categories, "
                             f"{len(scored)} scored questions; scores are not computed"))
        for i, cj in enumerate(s.conjoints):
            self.add(Feature("conjoint", cj.id or f"conjoint {i + 1}", "preserved_only", "none",
                             detail=f"{cj.kind} design metadata ({cj.confidence} confidence); "
                             "its randomization runs only as listed above"))


def executability(survey: Survey) -> ExecutabilityReport:
    """Inventory what administering ``survey`` involves; see the module docstring."""
    inv = _Inventory(survey)
    inv.flow(survey.flow)
    inv.quotas()
    inv.block_rows()
    inv.question_rows()
    inv.logic()
    inv.embedded()
    inv.pipe_rows()
    inv.preserved()
    return ExecutabilityReport(survey.name, list(dict.fromkeys(inv.rows)))
