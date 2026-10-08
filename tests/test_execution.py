"""ExecutionPolicy: strict by default; allow by code or code:location; implementations."""

from __future__ import annotations

import pytest

from surveyir.runtime.execution import (
    APPROXIMATIONS,
    IMPLEMENTED_BY,
    ExecutionError,
    ExecutionPolicy,
    approximation,
    javascript_affects,
)
from surveyir.runtime.trace import EXECUTION_AFFECTING, Allowed, Approximation

JS = approximation("javascript", "QID7", affects="assignment")


def test_codes_are_documented():
    for code, (affects, meaning) in APPROXIMATIONS.items():
        assert affects in EXECUTION_AFFECTING | {"none"} and meaning, code
    assert set(IMPLEMENTED_BY) <= set(APPROXIMATIONS)


def test_strict_raises_with_guidance():
    policy = ExecutionPolicy()
    assert not policy.permits(JS)
    with pytest.raises(ExecutionError) as err:
        policy.handle(JS)
    assert err.value.approximation == JS
    msg = str(err.value)
    for part in ("javascript at QID7", "implementations=", "allow=", "strict=False"):
        assert part in msg


@pytest.mark.parametrize("allow", ["javascript", "javascript:QID7"])
def test_allow_by_code_or_location(allow):
    policy = ExecutionPolicy(allow=frozenset({allow}))
    assert policy.permits(JS)
    assert policy.handle(JS) == Allowed("javascript", "QID7")
    assert policy.handle(JS) == Allowed("javascript", "QID7")  # stateless: every respondent


def test_allow_by_location_is_specific():
    policy = ExecutionPolicy(allow=frozenset({"javascript:QID8"}))
    with pytest.raises(ExecutionError):
        policy.handle(JS)


def test_location_with_colons():
    geo = approximation("logic.geo_ip", "loc://CountryName")
    policy = ExecutionPolicy(allow=frozenset({"logic.geo_ip:loc://CountryName"}))
    assert policy.handle(geo) == Allowed("logic.geo_ip", "loc://CountryName")


def test_permissive_records_without_raising():
    policy = ExecutionPolicy(strict=False)
    assert policy.permits(JS) and policy.handle(JS) is None
    assert ExecutionPolicy(strict=False, allow=frozenset({"javascript"})).handle(JS) == Allowed(
        "javascript", "QID7"
    )


def test_non_affecting_never_raises():
    note = Approximation("pipe.date", "CurrentDate", "none", "date in an unused field")
    policy = ExecutionPolicy(allow=frozenset({"pipe.date"}))
    assert ExecutionPolicy().permits(note) and ExecutionPolicy().handle(note) is None
    assert policy.handle(note) is None


def test_unknown_allow_code_is_rejected():
    with pytest.raises(ValueError, match="bogus"):
        ExecutionPolicy(allow=frozenset({"bogus"}))


def test_implementations():
    def service(node, state):
        return {"x": "1"}

    def script(state):
        return {"arm": "A"}

    policy = ExecutionPolicy(implementations={
        "web_service": service,
        "javascript": {"QID7": script},
        "embedded": {"PROLIFIC_PID": "p1"},
        "location": {"CountryName": "Australia"},
    })
    assert policy.implementation("web_service", "FL_3") is service
    assert policy.implementation("javascript", "QID7") is script
    assert policy.implementation("javascript", "QID8") is None
    assert policy.implementation("embedded.unset", "PROLIFIC_PID") == "p1"  # by code
    assert policy.implementation("location", "loc://CountryName") == "Australia"
    assert policy.implementation("library_block", "FL_9") is None


def test_from_args():
    policy = ExecutionPolicy.from_args(None, ["javascript,web_service", "logic.geo_ip:x"])
    assert policy.strict and policy.allow == {"javascript", "web_service", "logic.geo_ip:x"}
    assert not ExecutionPolicy.from_args(False, None).strict


def test_javascript_affects():
    template = (
        "Qualtrics.SurveyEngine.addOnload(function()\n{\n\t/*Place your JavaScript here*/\n\n});"
        "\nQualtrics.SurveyEngine.addOnReady(function() { // nothing\n});"
    )
    assert javascript_affects(None) is None and javascript_affects(template) is None
    commented = template + "\n/* Qualtrics.SurveyEngine.setEmbeddedData('a', 1); */"
    assert javascript_affects(commented) is None
    assert javascript_affects("jQuery('#x').hide();") == "exposure"
    sets = "var u = 'https://x.org'; Qualtrics.SurveyEngine.setEmbeddedData('arm', u);"
    assert javascript_affects(sets) == "assignment"
    assert javascript_affects("x = Math.random();") == "assignment"
    assert javascript_affects("Qualtrics.SurveyEngine.setJSEmbeddedData('a', 1)") == "assignment"
