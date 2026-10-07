"""Shared base classes for the surveyir intermediate representation (IR)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Version of the IR schema. Bump the minor version for additive changes and
#: the major version for breaking ones. Serialized surveys carry this value.
SCHEMA_VERSION = "0.2"


class IRModel(BaseModel):
    """Base for every IR node.

    Unknown attributes are rejected at construction time so typos fail loudly;
    source-format data that has no typed home goes in ``extras`` instead.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)


class Extensible(IRModel):
    """An IR node that can carry source-format data it has no typed field for.

    Loaders put every source key they do not map into ``extras`` verbatim, so a
    survey can always be inspected (or re-exported) without information loss.
    """

    extras: dict[str, Any] = Field(
        default_factory=dict,
        description="Source-format fields with no typed equivalent, preserved verbatim.",
    )
