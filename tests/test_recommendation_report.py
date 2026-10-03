from __future__ import annotations

import pytest

from tradingagents.client_constraints import evaluate_client_constraints
from tradingagents.client_profile import extract_client_profile
from tradingagents.recommendation_report import (
    RecommendationReport,
    build_recommendation_report,
    render_recommendation_report,
)


def _state():
    return {
        "company_of_interest": "NVDA",
        "final_rating": "Hold",
        "market_report": "Technical context",
        "fundamentals_report": "Fundamental context",
        "news_report": "News context",
        "sentiment_report": "Sentiment context",
        "investment_debate_state": {
            "bull_history": "Bull Analyst: Growth upside remains strong.",
            "bear_history": "Bear Analyst: Valuation and execution risks are elevated.",
        },
        "risk_debate_state": {
            "aggressive_history": "Aggressive Analyst: upside momentum possible.",
            "conservative_history": "Conservative Analyst: protect downside.",
            "neutral_history": "Neutral Analyst: balance risk and return.",
        },
        "trader_investment_plan": "Trader plan",
        "final_trade_decision": "**Rating**: Hold\n\nDecision text",
        "portfolio_context": "Portfolio at the analysis date: flat",
        "client_profile_context": "Client profile context block",
        "client_profile_data": extract_client_profile(
            "All ten payments must be funded by the investment portfolio with a high degree of certainty. "
            "Teams may not rely on co-sponsors or grants to meet this requirement."
        ).model_dump(),
    }


@pytest.mark.unit
def test_recommendation_report_composes_expected_fields():
    state = _state()
    violations = evaluate_client_constraints(state)
    report = build_recommendation_report(state, violations)
    assert isinstance(report, RecommendationReport)
    assert report.security == "NVDA"
    assert report.recommendation == "Hold"
    assert "Fundamental context" in report.fundamental_analysis
    assert report.client_fit.startswith("Client profile was provided")
    assert "Portfolio Manager decision" in report.evidence_sources


@pytest.mark.unit
def test_constraint_detector_flags_outside_funding_dependency():
    state = _state()
    state["final_trade_decision"] = "Use outside funding for payments if markets weaken."
    violations = evaluate_client_constraints(state)
    assert any(v.code == "outside_funding_dependency" for v in violations)


@pytest.mark.unit
def test_report_forces_review_when_blocking_constraints_exist():
    state = _state()
    state["final_trade_decision"] = "Use outside funding for payments if markets weaken."
    report = build_recommendation_report(state, evaluate_client_constraints(state))
    assert report.recommendation == "REVIEW"
    assert "Blocking client-constraint violations" in report.uncertainty


@pytest.mark.unit
def test_rendered_report_contains_required_sections():
    state = _state()
    report = build_recommendation_report(state, evaluate_client_constraints(state))
    text = render_recommendation_report(report)
    assert "**Security**: NVDA" in text
    assert "### Client Fit" in text
    assert "### Evidence / Sources" in text
