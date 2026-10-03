"""Structured recommendation report composed from the completed run state."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from tradingagents.agents.rating import run_rating
from tradingagents.client_constraints import ConstraintViolation


class RecommendationReport(BaseModel):
    security: str
    recommendation: str
    confidence: str
    uncertainty: str
    fundamental_analysis: str
    technical_analysis: str
    news_sentiment_analysis: str
    bull_case: str
    bear_case: str
    client_fit: str
    portfolio_impact: str
    key_risks: str
    thesis_invalidation: str
    evidence_sources: list[str] = Field(default_factory=list)
    client_constraint_violations: list[dict] = Field(default_factory=list)
    timestamp_utc: str


def _trim(text: str, limit: int = 1400) -> str:
    value = (text or "").strip()
    if not value:
        return "Not provided by this run."
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _debate_side(history: str, prefix: str) -> str:
    lines = [line.strip() for line in (history or "").splitlines() if line.strip().startswith(prefix)]
    if not lines:
        return "Not provided by this run."
    return _trim("\n".join(lines[-3:]), limit=1200)


def _extract_key_risks(final_state: dict) -> str:
    risk = final_state.get("risk_debate_state") or {}
    bits = []
    for key in ("conservative_history", "neutral_history", "aggressive_history"):
        text = (risk.get(key) or "").strip()
        if text:
            bits.append(text)
    if not bits:
        return "Not provided by this run."
    return _trim("\n\n".join(bits[-2:]), limit=1400)


def build_recommendation_report(final_state: dict, violations: list[ConstraintViolation]) -> RecommendationReport:
    rating = run_rating(final_state)
    profile_context = (final_state.get("client_profile_context") or "").strip()
    profile_present = bool(profile_context)
    confidence = "Medium"
    uncertainty = (
        "Model-based recommendation with market and prompt uncertainty; validate assumptions before action."
    )
    if rating == "REVIEW":
        confidence = "Low"
        uncertainty = "No parseable final rating detected; requires human review before use."

    sources = []
    if final_state.get("fundamentals_report"):
        sources.append("Fundamentals Analyst report")
    if final_state.get("market_report"):
        sources.append("Market/Technical Analyst report")
    if final_state.get("news_report"):
        sources.append("News Analyst report")
    if final_state.get("sentiment_report"):
        sources.append("Sentiment Analyst report")
    if final_state.get("investment_debate_state"):
        sources.append("Bull/Bear research debate")
    if final_state.get("risk_debate_state"):
        sources.append("Risk debate")
    if final_state.get("trader_investment_plan"):
        sources.append("Trader proposal")
    if final_state.get("final_trade_decision"):
        sources.append("Portfolio Manager decision")

    return RecommendationReport(
        security=str(final_state.get("company_of_interest", "")),
        recommendation=rating,
        confidence=confidence,
        uncertainty=uncertainty,
        fundamental_analysis=_trim(final_state.get("fundamentals_report", "")),
        technical_analysis=_trim(final_state.get("market_report", "")),
        news_sentiment_analysis=_trim(
            "\n\n".join(
                part for part in (
                    final_state.get("news_report", ""),
                    final_state.get("sentiment_report", ""),
                ) if part
            )
        ),
        bull_case=_debate_side((final_state.get("investment_debate_state") or {}).get("bull_history", ""), "Bull Analyst:"),
        bear_case=_debate_side((final_state.get("investment_debate_state") or {}).get("bear_history", ""), "Bear Analyst:"),
        client_fit=(
            "Client profile was provided and integrated in agent prompts. "
            "Validate extracted hard constraints and suitability narrative before final submission."
            if profile_present else
            "Client profile not provided; suitability to specific client constraints is uncertain."
        ),
        portfolio_impact=_trim(
            "\n\n".join(
                part for part in (
                    final_state.get("portfolio_context", ""),
                    final_state.get("trader_investment_plan", ""),
                    final_state.get("final_trade_decision", ""),
                ) if part
            )
        ),
        key_risks=_extract_key_risks(final_state),
        thesis_invalidation=(
            "Invalidated if new evidence contradicts the core thesis in fundamentals, "
            "technical structure, or news/sentiment regime."
        ),
        evidence_sources=sources,
        client_constraint_violations=[v.model_dump() for v in violations],
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )


def render_recommendation_report(report: RecommendationReport) -> str:
    lines = [
        f"**Security**: {report.security}",
        f"**Recommendation**: {report.recommendation}",
        f"**Confidence**: {report.confidence}",
        f"**Uncertainty**: {report.uncertainty}",
        "",
        "### Client Fit",
        report.client_fit,
        "",
        "### Portfolio Impact",
        report.portfolio_impact,
        "",
        "### Key Risks",
        report.key_risks,
        "",
        "### Thesis Invalidation",
        report.thesis_invalidation,
        "",
        "### Evidence / Sources",
    ]
    lines.extend([f"- {source}" for source in report.evidence_sources] or ["- Not listed"])
    if report.client_constraint_violations:
        lines.extend(["", "### Client Constraint Violations"])
        for item in report.client_constraint_violations:
            lines.append(f"- [{item['severity']}] {item['code']}: {item['message']}")
    lines.extend(["", f"**Timestamp (UTC)**: {report.timestamp_utc}"])
    return "\n".join(lines)
