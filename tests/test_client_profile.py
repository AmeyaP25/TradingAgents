"""Client-profile context: parse, preserve, and thread into agent runs."""

from __future__ import annotations

import json

import pytest

from tradingagents.agents.context import get_client_profile_context_from_state
from tradingagents.client_profile import (
    ClientProfile,
    apply_client_profile_overrides,
    extract_client_profile,
    load_client_profile,
    load_overrides,
)

CASE = """
Potential client, Laura Gao, plans to invest $300,000 at the beginning of 2027.
She will contribute an additional $150,000 at the beginning of 2028.
Operating commitment: ten annual payments of $50,000 from 2033 through 2042.
All ten payments must be funded by the investment portfolio with a high degree of certainty.
Teams may not rely on co-sponsors or grants to meet this requirement.
"""


@pytest.mark.unit
def test_extraction_preserves_raw_case_text():
    profile = extract_client_profile(CASE)
    assert "Laura Gao" in profile.raw_case_study_text
    assert profile.client_name.value == "Laura Gao"


@pytest.mark.unit
def test_extraction_captures_contributions_and_operating_schedule():
    profile = extract_client_profile(CASE)
    amounts = sorted(int(flow.amount or 0) for flow in profile.cash_flows if flow.amount is not None)
    assert 50000 in amounts and 150000 in amounts and 300000 in amounts
    assert any(flow.frequency == "annual" and flow.count == 10 for flow in profile.cash_flows)
    assert profile.co_sponsor_communication_year.status in {"unknown", "explicit"}


@pytest.mark.unit
def test_extraction_tracks_timeline_and_operating_reserve_requirements():
    case = CASE + "\n2026 is Year 0.\n2031 is Year 5.\nAt the beginning of 2033, Laura will set aside an operating reserve."
    profile = extract_client_profile(case)
    assert profile.timeline_milestones["2026"] == 0
    assert profile.year_zero_reference.value == "Year 0 maps to 2026"
    assert profile.operating_reserve_required.status == "explicit"


@pytest.mark.unit
def test_absent_client_profile_context_is_reported_as_not_provided():
    notice = get_client_profile_context_from_state({"company_of_interest": "AAPL"})
    assert "not provided" in notice.lower()


@pytest.mark.unit
def test_rendered_client_profile_context_reaches_state_helper():
    block = get_client_profile_context_from_state(
        {"client_profile_context": "CLIENT_PROFILE_MARKER", "company_of_interest": "AAPL"}
    )
    assert block == "CLIENT_PROFILE_MARKER"


@pytest.mark.unit
def test_load_rejects_malformed_profile_file(tmp_path):
    bad = tmp_path / "client.json"
    bad.write_text(json.dumps({"client_name": {"value": "x"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="client profile"):
        load_client_profile(bad)


@pytest.mark.unit
def test_overrides_patch_a_profile_field():
    profile = extract_client_profile(CASE)
    updated = apply_client_profile_overrides(profile, {"risk_tolerance": {"value": "Moderate"}})
    assert updated.risk_tolerance.value == "Moderate"
    assert updated.client_name.value == profile.client_name.value


@pytest.mark.unit
def test_load_overrides_requires_json_object(tmp_path):
    bad = tmp_path / "overrides.json"
    bad.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_overrides(bad)


def _bare_graph(tmp_path):
    from tradingagents.graph.propagation import Propagator
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.memory import TradingMemoryLog

    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"memory_log_path": str(tmp_path / "m.md"), "max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
    graph.memory_log = TradingMemoryLog(graph.config)
    graph.propagator = Propagator()
    graph.selected_analysts = ["market"]
    graph.settle_pending = lambda t: None
    graph.resolve_instrument_context = lambda t, a="stock", d=None: ""
    graph._memory_as_of = lambda d: None
    return graph


@pytest.mark.unit
def test_create_run_state_renders_client_profile_once(tmp_path):
    graph = _bare_graph(tmp_path)
    profile = extract_client_profile(CASE)
    state = graph.create_run_state("AAPL", "2026-08-14", client_profile=profile)
    assert "Laura Gao" in state["client_profile_context"]
    assert state["client_profile_data"]["client_name"]["value"] == "Laura Gao"
    assert graph.create_run_state("AAPL", "2026-08-14")["client_profile_context"] == ""


@pytest.mark.unit
def test_checkpoint_signature_changes_with_client_profile(tmp_path):
    graph = _bare_graph(tmp_path)
    none = graph._run_signature("stock")
    known = graph._run_signature("stock", client_profile=extract_client_profile(CASE))
    assert none != known


@pytest.mark.unit
def test_cli_case_study_parses_and_passes_profile(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as m

    case_path = tmp_path / "case.txt"
    case_path.write_text(CASE, encoding="utf-8")
    calls = []
    monkeypatch.setattr(m, "run_analysis", lambda **k: calls.append(k))

    result = CliRunner().invoke(m.app, ["--client-case-study", str(case_path)], input="Y\n")

    assert result.exit_code == 0
    assert isinstance(calls[0]["client_profile"], ClientProfile)
    assert calls[0]["client_profile"].client_name.value == "Laura Gao"


@pytest.mark.unit
def test_cli_case_study_applies_overrides_before_run(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as m

    case_path = tmp_path / "case.txt"
    case_path.write_text(CASE, encoding="utf-8")
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"risk_tolerance": {"value": "Moderate"}}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(m, "run_analysis", lambda **k: calls.append(k))

    result = CliRunner().invoke(
        m.app,
        ["--client-case-study", str(case_path), "--client-profile-overrides", str(overrides)],
        input="Y\nY\n",
    )

    assert result.exit_code == 0
    assert calls[0]["client_profile"].risk_tolerance.value == "Moderate"
