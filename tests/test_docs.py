"""The docs and examples run, and print what the docs say they print."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.doc_runner import ROOT, doc_files, run_file

EXAMPLES = sorted(p for p in (ROOT / "examples").glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", doc_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_doc_examples_print_what_they_show(path):
    assert run_file(path) == [], "run `uv run python scripts/update_docs.py` to refresh"


@pytest.mark.parametrize(
    "path", [p for p in EXAMPLES if p.name != "llm_respondents.py"], ids=lambda p: p.name
)
def test_example_scripts_run(path):
    result = subprocess.run(
        [sys.executable, str(path)], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


class FakeClient:
    """Stands in for anthropic.Anthropic(): answers with the first allowed value."""

    def __init__(self, refuse: bool = False) -> None:
        self.requests: list[dict] = []
        self.refuse = refuse
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        if self.refuse:
            return SimpleNamespace(stop_reason="refusal", content=[])
        schema = request["output_config"]["format"]["schema"]["properties"]["answer"]
        answer = _first_valid(schema)
        text = SimpleNamespace(type="text", text=json.dumps({"answer": answer}))
        return SimpleNamespace(stop_reason="end_turn", content=[text])


def _first_valid(schema: dict):
    if "enum" in schema:
        return schema["enum"][0]
    if schema["type"] == "array":
        return [_first_valid(schema["items"])]
    if schema["type"] == "object":
        return {k: _first_valid(v) for k, v in schema["properties"].items()}
    return "I will not use AI to answer this survey."


def test_llm_example_with_a_fake_client():
    sys.path.insert(0, str(ROOT))
    import surveyir
    from examples.llm_respondents import MODEL, claude_answerer
    from surveyir.runtime import Display, ExecutionError, Simulator
    from surveyir.runtime.trace import Allowed, Response

    survey = surveyir.load(ROOT / "tests/fixtures/qualtrics/obedient_twins.qsf")
    client = FakeClient()
    persona = claude_answerer(client, "You are a test persona.")
    with pytest.raises(ExecutionError, match="answer.invalid at QID14"):  # 100 characters
        Simulator(survey, seed=1).respondent(persona)
    client = FakeClient()
    persona = claude_answerer(client, "You are a test persona.")
    run = Simulator(survey, seed=1, allow={"answer.invalid"}).respondent(persona)
    assert run.answers and len(run.displayed) > 2 and not run.privileged
    assert Allowed("answer.invalid", "QID14") in run.audit
    assert any("Write at least 100 characters." in m["content"]
               for r in client.requests for m in r["messages"])
    first, last = client.requests[0], client.requests[-1]
    assert first["model"] == MODEL and first["fallbacks"] == "default"
    assert "You are a test persona." in first["system"]
    # one conversation: every earlier answer is an assistant turn, and the screens
    # shown before (text-only ones included) are in the user turns
    roles = [m["role"] for m in last["messages"]]
    assert roles == ["user", "assistant"] * (len(client.requests) - 1) + ["user"]
    said = "\n".join(m["content"] for m in last["messages"] if m["role"] == "user")
    asked = [o.seq for o in run.trace if isinstance(o, Response)][-1]  # the last request
    screens = [o for o in run.trace if isinstance(o, Display) and o.kind == "descriptive"]
    assert screens and all(d.text in said for d in screens if d.seq < asked)
    assert "s_1" not in said and "FL_" not in said  # no assignment fields or flow ids

    refused = Simulator(survey, seed=1).respondent(claude_answerer(FakeClient(refuse=True), "x"))
    assert refused.answers == {}

