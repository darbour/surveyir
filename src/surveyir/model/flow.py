"""Survey flow: the tree that decides which blocks a respondent sees, in what order."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import Field

from .base import Extensible
from .logic import Condition
from .text import Text

EmbeddedSource = Literal["custom", "recipient", "panel", "random", "other"]
Termination = Literal["default", "message", "redirect", "other"]


class EmbeddedField(Extensible):
    """A variable set in the flow (Qualtrics 'embedded data')."""

    name: str
    source: EmbeddedSource = Field(
        description="'custom' = value set here; 'recipient'/'panel' = supplied per respondent "
        "(e.g. a URL parameter or contact list); 'random' = drawn at run time."
    )
    value: Text | None = None
    variable_type: str | None = None


class _Node(Extensible):
    id: str = Field(description="Source flow id, e.g. 'FL_12'.")
    description: str | None = None


class BlockNode(_Node):
    type: Literal["block"] = "block"
    block_id: str


class EmbeddedDataNode(_Node):
    type: Literal["embedded_data"] = "embedded_data"
    fields: list[EmbeddedField]


class BranchNode(_Node):
    """Children run only if ``condition`` holds."""

    type: Literal["branch"] = "branch"
    condition: Condition | None = Field(
        description="None only if the source branch had no readable logic (see diagnostics)."
    )
    children: list[FlowNode]


class RandomizerNode(_Node):
    """Children are shuffled; optionally only ``subset_size`` of them run."""

    type: Literal["randomizer"] = "randomizer"
    subset_size: int | None = None
    even_presentation: bool = False
    children: list[FlowNode]


class GroupNode(_Node):
    type: Literal["group"] = "group"
    children: list[FlowNode]


class EndSurveyNode(_Node):
    type: Literal["end_survey"] = "end_survey"
    termination: Termination = "default"
    redirect_url: str | None = None
    message_id: str | None = None
    response_flag: str | None = Field(
        default=None, description="Status recorded on the response, e.g. 'Screened', 'QuotaMet'."
    )
    ignore_response: bool = Field(
        default=False, description="The response is discarded (not recorded)."
    )


class WebServiceNode(_Node):
    """Calls an external URL at run time and may set embedded data from the response."""

    type: Literal["web_service"] = "web_service"
    url: str | None = None
    method: str | None = None
    request_params: dict[str, str] = Field(default_factory=dict)
    response_map: dict[str, str] = Field(
        default_factory=dict, description="Response key -> embedded-data field it sets."
    )

    @property
    def sets_fields(self) -> list[str]:
        return list(self.response_map.values())


class AuthenticatorNode(_Node):
    type: Literal["authenticator"] = "authenticator"
    children: list[FlowNode] = Field(default_factory=list)


class TableOfContentsNode(_Node):
    type: Literal["table_of_contents"] = "table_of_contents"
    children: list[FlowNode] = Field(default_factory=list)


class LibraryBlockNode(_Node):
    """A block pulled in from a Qualtrics library at run time (``ReferenceSurvey``).

    Its questions are not part of the exported file, so they cannot be shown.
    """

    type: Literal["library_block"] = "library_block"
    reference_id: str = Field(description="Library survey/block id, e.g. 'LS_abc'.")
    library_id: str | None = None


class QuotaNode(_Node):
    """Point in the flow where quotas are checked (see ``Survey.quotas``)."""

    type: Literal["quota"] = "quota"


class UnsupportedNode(_Node):
    """A flow element surveyir does not model yet. ``extras`` holds it verbatim."""

    type: Literal["unsupported"] = "unsupported"
    source_type: str
    children: list[FlowNode] = Field(default_factory=list)


FlowNode = Annotated[
    BlockNode
    | EmbeddedDataNode
    | BranchNode
    | RandomizerNode
    | GroupNode
    | EndSurveyNode
    | WebServiceNode
    | AuthenticatorNode
    | TableOfContentsNode
    | LibraryBlockNode
    | QuotaNode
    | UnsupportedNode,
    Field(discriminator="type"),
]

for _model in (
    BranchNode,
    RandomizerNode,
    GroupNode,
    AuthenticatorNode,
    TableOfContentsNode,
    UnsupportedNode,
):
    _model.model_rebuild()


def walk(nodes: list[FlowNode]) -> Iterator[FlowNode]:
    """Yield every node in ``nodes`` and their descendants, depth first, in order."""
    for node in nodes:
        yield node
        children = getattr(node, "children", None)
        if children:
            yield from walk(children)
