"""Run a survey: experimental design, logic evaluation and respondent simulation.

from surveyir.runtime import Simulator, RandomAnswerer, design

d = design(survey)              # what is randomized, and how
print(d.summary())
sim = Simulator(survey, seed=1)
runs = sim.run(500, RandomAnswerer(seed=2))
rows = [r.row(survey) for r in runs]   # columns match response_columns()
"""

from .design import Arm, Design, Factor, Opaque, OrderRandomization, RandomValue, design
from .logic import evaluate
from .pipes import render, resolve_pipe
from .state import Answer, LoopContext, RespondentState
from .walker import (
    Answerer,
    ChoiceView,
    Counterbalancer,
    QuestionView,
    RandomAnswerer,
    RespondentRun,
    ScreenerAwareAnswerer,
    Simulator,
    arrange,
    no_answer,
)

__all__ = [
    "Answer",
    "Answerer",
    "Arm",
    "ChoiceView",
    "Counterbalancer",
    "Design",
    "Factor",
    "LoopContext",
    "Opaque",
    "OrderRandomization",
    "QuestionView",
    "RandomAnswerer",
    "RandomValue",
    "RespondentRun",
    "RespondentState",
    "ScreenerAwareAnswerer",
    "Simulator",
    "arrange",
    "design",
    "evaluate",
    "no_answer",
    "render",
    "resolve_pipe",
]
