"""surveyir: a lossless intermediate representation for survey instruments.

Quick start::

    import surveyir

    survey = surveyir.load("study.qsf")            # Qualtrics export -> Survey
    print(surveyir.export(survey, "markdown"))      # readable instrument
    survey.to_json()                                # canonical IR as JSON
"""

import logging

from .columns import (
    Column,
    ColumnOptions,
    column_label,
    infer_options,
    read_header,
    response_columns,
    response_header_rows,
)
from .describe import describe_condition
from .exporters import Exporter, available_exporters, export, get_exporter
from .loaders import available_loaders, load
from .loaders.qualtrics import LoadError, load_qsf
from .model import SCHEMA_VERSION, Survey
from .runtime import RandomAnswerer, Simulator, design, write_responses_csv

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "SCHEMA_VERSION",
    "RandomAnswerer",
    "Simulator",
    "Column",
    "ColumnOptions",
    "Exporter",
    "LoadError",
    "Survey",
    "available_exporters",
    "available_loaders",
    "column_label",
    "describe_condition",
    "design",
    "export",
    "get_exporter",
    "infer_options",
    "read_header",
    "load",
    "load_qsf",
    "response_columns",
    "response_header_rows",
    "write_responses_csv",
]
