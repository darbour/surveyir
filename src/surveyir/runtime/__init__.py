"""Run a survey: experimental design, logic evaluation and respondent simulation.

from surveyir.runtime import Simulator, RandomAnswerer, design

d = design(survey)              # what is randomized, and how
print(d.summary())
sim = Simulator(survey, seed=1)
runs = sim.run(500, RandomAnswerer(seed=2))
print(transcript(runs[0].trace))   # what the first respondent saw and answered
rows = [r.row(survey) for r in runs]   # columns match response_columns()
"""

from .design import (
    Arm,
    Design,
    Factor,
    FactorAnnotation,
    Opaque,
    OrderRandomization,
    RandomValue,
    design,
)
from .executability import ExecutabilityReport, executability
from .execution import APPROXIMATIONS, ExecutionError, ExecutionPolicy, javascript_affects
from .logic import UNKNOWN, evaluate
from .pipes import render, resolve_pipe
from .state import Answer, LoopContext, RespondentState
from .trace import AuditEvent, Display, Observation, transcript
from .walker import (
    Answerer,
    ChoiceView,
    Counterbalancer,
    QuestionView,
    RandomAnswerer,
    Respondent,
    RespondentRun,
    ResponseContext,
    ScreenerAwareAnswerer,
    Simulator,
    arrange,
    no_answer,
)

__all__ = [
    "APPROXIMATIONS",
    "ExecutabilityReport",
    "ExecutionError",
    "ExecutionPolicy",
    "FactorAnnotation",
    "UNKNOWN",
    "executability",
    "javascript_affects",
    "Answer",
    "Answerer",
    "Arm",
    "AuditEvent",
    "ChoiceView",
    "Counterbalancer",
    "Design",
    "Display",
    "Factor",
    "LoopContext",
    "Observation",
    "Opaque",
    "OrderRandomization",
    "QuestionView",
    "RandomAnswerer",
    "RandomValue",
    "Respondent",
    "RespondentRun",
    "RespondentState",
    "ResponseContext",
    "ScreenerAwareAnswerer",
    "Simulator",
    "arrange",
    "design",
    "evaluate",
    "no_answer",
    "render",
    "resolve_pipe",
    "transcript",
]
