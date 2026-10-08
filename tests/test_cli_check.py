"""``surveyir check`` and the execution flags on ``surveyir simulate``."""

from __future__ import annotations

import json

import pytest

from surveyir.cli import main
from tests.conftest import FIXTURES

DONORS = str(FIXTURES / "promiscuous_donors.qsf")
HIRING = str(FIXTURES / "hiring_algorithms.qsf")


def test_check_prints_summary_and_exits_zero(capsys):
    assert main(["check", DONORS]) == 0
    out = capsys.readouterr().out
    assert "Needs implementation:" in out and "QID481" in out and "Blocking" not in out


def test_check_strict_blocks_and_explains(capsys):
    assert main(["check", DONORS, "--strict"]) == 1
    out = capsys.readouterr().out
    assert "Blocking under strict execution (1):" in out
    assert "implementations={'javascript': {'QID481': ...}}" in out and "--permissive" in out


@pytest.mark.parametrize("allow", [["javascript"], ["javascript:QID481"]])
def test_check_strict_with_allow_passes(allow, capsys):
    args = [a for code in allow for a in ("--allow", code)]
    assert main(["check", DONORS, "--strict", *args]) == 0


def test_check_strict_clean_survey(capsys):
    assert main(["check", HIRING, "--strict"]) == 0


def test_check_json(capsys):
    assert main(["check", DONORS, "--strict", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["needs_implementation"] == 1
    assert [(b["code"], b["location"]) for b in data["blocking"]] == [("javascript", "QID481")]
    assert main(["check", DONORS, "--json"]) == 0
    assert "blocking" not in json.loads(capsys.readouterr().out)


def test_unknown_allow_code_is_an_error():
    with pytest.raises(SystemExit, match="bogus"):
        main(["check", DONORS, "--allow", "bogus"])


def test_simulate_flags(capsys):
    assert main(["simulate", HIRING, "-n", "1", "--seed", "1"]) == 0  # strict by default
    assert capsys.readouterr().out.startswith("StartDate,")
    assert main(["simulate", DONORS, "-n", "2", "--seed", "1"]) == 1
    err = capsys.readouterr().err
    assert "javascript at QID481" in err and "surveyir check --strict" in err
    for flags in (["--permissive"], ["--allow", "javascript:QID481"], ["--strict", "--allow",
                                                                         "javascript"]):
        assert main(["simulate", DONORS, "-n", "2", "--seed", "1", *flags]) == 0
        assert capsys.readouterr().out.startswith("StartDate,")
