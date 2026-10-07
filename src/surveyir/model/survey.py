"""The top-level ``Survey`` document."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

from pydantic import Field

from .base import SCHEMA_VERSION, Extensible, IRModel
from .block import Block
from .flow import BlockNode, FlowNode, walk
from .logic import Condition
from .question import NON_RESPONSE_KINDS, Question


class Diagnostic(IRModel):
    """Something a loader could not represent faithfully."""

    level: Literal["info", "warning", "error"]
    code: str = Field(description="Stable machine-readable code, e.g. 'unsupported-question'.")
    message: str
    location: str | None = Field(default=None, description="Source id/path, e.g. 'QID12'.")


class SourceInfo(IRModel):
    format: str = Field(description="e.g. 'qualtrics.qsf'.")
    format_version: str | None = None
    loader: str = Field(description="Loader and version that produced this document.")


class ScoringCategory(IRModel):
    id: str
    name: str
    description: str | None = None


class Quota(Extensible):
    """A respondent-count limit, and what happens when it fills."""

    id: str
    name: str
    limit: int | None = Field(default=None, description="Number of respondents allowed.")
    condition: Condition | None = Field(
        default=None, description="Which respondents count toward the quota."
    )
    action: str | None = Field(
        default=None,
        description="Source action when full, e.g. 'EndCurrentSurvey', 'ForBranching'.",
    )
    over_quota_action: str | None = None


class ConjointFeature(IRModel):
    name: str
    levels: list[str] = Field(default_factory=list)
    allow_elimination: bool | None = None


class ConjointDesign(Extensible):
    """A conjoint experiment found in the survey.

    ``self_explicated_legacy`` is Qualtrics' old built-in conjoint element (``CJ``);
    ``diy_js`` is the common do-it-yourself design in which question JavaScript
    randomizes attribute levels into embedded data (Strezhnev's Conjoint Survey
    Design Tool and the cjoint/projoint R packages use ``F-``/``K-`` field names).
    Qualtrics' Conjoint XM projects keep their design outside the .qsf.
    """

    kind: Literal["self_explicated_legacy", "diy_js"]
    id: str | None = None
    features: list[ConjointFeature] = Field(default_factory=list)
    tasks: int | None = Field(default=None, description="Choice tasks per respondent.")
    profiles: int | None = Field(default=None, description="Profiles shown per task.")
    field_prefix: str | None = Field(
        default=None, description="Embedded-data prefix, e.g. 'F' for fields like F-1-2-3."
    )
    question_ids: list[str] = Field(
        default_factory=list, description="Questions that define or display the design."
    )
    confidence: Literal["high", "medium", "low"] = "medium"
    source: str = Field(description="Where in the file the design was found.")


class Survey(Extensible):
    """A complete survey instrument.

    ``blocks`` and ``questions`` are keyed by id and kept in source order. The
    ``flow`` tree decides what a respondent actually sees. Blocks or questions
    that are never reachable from the flow are still present (``unused_*``
    helpers find them); trashed elements are dropped at load time.
    """

    schema_version: str = SCHEMA_VERSION
    id: str
    name: str
    description: str | None = None
    language: str | None = None
    languages: list[str] = Field(
        default_factory=list, description="Additional languages with translations."
    )
    created: str | None = None
    modified: str | None = None
    source: SourceInfo
    blocks: dict[str, Block] = Field(default_factory=dict)
    questions: dict[str, Question] = Field(default_factory=dict)
    flow: list[FlowNode] = Field(default_factory=list)
    scoring_categories: list[ScoringCategory] = Field(default_factory=list)
    quotas: list[Quota] = Field(default_factory=list)
    conjoints: list[ConjointDesign] = Field(default_factory=list)
    options: dict[str, Any] = Field(
        default_factory=dict, description="Survey-wide platform options, source format."
    )
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    # ------------------------------------------------------------------ lookups

    def question(self, ref: str) -> Question:
        """Look up a question by id (``QID12``) or export tag (``age``)."""
        if ref in self.questions:
            return self.questions[ref]
        for q in self.questions.values():
            if q.export_tag == ref:
                return q
        raise KeyError(ref)

    def block_questions(self, block_id: str) -> list[Question]:
        """Questions of a block in order; empty if the block does not exist."""
        block = self.blocks.get(block_id)
        if block is None:
            return []
        return [self.questions[qid] for qid in block.question_ids if qid in self.questions]

    def walk_flow(self) -> Iterator[FlowNode]:
        return walk(self.flow)

    def flow_block_ids(self) -> list[str]:
        """Block ids referenced from the flow, in first-appearance order."""
        seen: dict[str, None] = {}
        for node in self.walk_flow():
            if isinstance(node, BlockNode):
                seen.setdefault(node.block_id, None)
        return list(seen)

    def iter_questions(self, *, responses_only: bool = False) -> Iterator[Question]:
        """Questions in flow order (every reachable block once), then unused blocks."""
        order = self.flow_block_ids()
        order += [bid for bid in self.blocks if bid not in order]
        for bid in order:
            for q in self.block_questions(bid):
                if responses_only and q.kind in NON_RESPONSE_KINDS:
                    continue
                yield q

    def unused_block_ids(self) -> list[str]:
        used = set(self.flow_block_ids())
        return [bid for bid in self.blocks if bid not in used]

    # ------------------------------------------------------------------ io

    def to_json(self, *, indent: int | None = 2) -> str:
        return self.model_dump_json(indent=indent, exclude_defaults=False)

    @classmethod
    def from_json(cls, data: str | bytes) -> Survey:
        return cls.model_validate_json(data)
