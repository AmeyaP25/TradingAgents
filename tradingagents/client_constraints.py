"""Deterministic checks of client-level hard constraints.

These checks are intentionally conservative and text-grounded: they only flag
violations that can be detected from explicit language in the case profile and
the generated recommendations, and avoid inventing unmet requirements.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ConstraintViolation(BaseModel):
    code: str
    severity: Literal["blocking", "warning"]
    message: str
    evidence: str | None = None


def evaluate_client_constraints(final_state: dict) -> list[ConstraintViolation]:
    """Return detected hard-constraint violations from a completed run state."""
    profile = final_state.get("client_profile_data") or {}
    constraints = profile.get("hard_constraints") or []
    if not constraints:
        return []

    text = " ".join(
        str(final_state.get(key, "") or "")
        for key in (
            "investment_plan",
            "trader_investment_plan",
            "final_trade_decision",
            "risk_debate_state",
        )
    ).lower()

    violations: list[ConstraintViolation] = []

    def has_hard_phrase(phrase: str) -> bool:
        return any(
            (item.get("kind") == "hard")
            and phrase in str(item.get("description", "")).lower()
            for item in constraints
            if isinstance(item, dict)
        )

    if has_hard_phrase("may not rely on co-sponsors") and any(
        phrase in text
        for phrase in ("rely on co-sponsor", "rely on grant", "outside funding for payments")
    ):
        violations.append(ConstraintViolation(
            code="outside_funding_dependency",
            severity="blocking",
            message=(
                "Recommendation appears to rely on outside funding for operating payments, "
                "which is prohibited by the case constraints."
            ),
            evidence="Detected phrases indicating reliance on co-sponsors/grants.",
        ))

    if has_hard_phrase("beginning of the applicable year") and "end of year" in text:
        violations.append(ConstraintViolation(
            code="cashflow_timing_mismatch",
            severity="warning",
            message=(
                "Recommendation references end-of-year timing while the case requires "
                "beginning-of-year cash-flow timing."
            ),
            evidence="Detected 'end of year' wording in recommendation text.",
        ))

    if has_hard_phrase("all ten payments must be funded by the investment portfolio") and any(
        phrase in text
        for phrase in ("cannot fund all ten", "unable to fund all ten", "partial funding of payments")
    ):
        violations.append(ConstraintViolation(
            code="operating_commitment_not_fully_funded",
            severity="blocking",
            message=(
                "Recommendation text indicates incomplete funding of the ten required operating payments."
            ),
            evidence="Detected language indicating partial or failed funding.",
        ))

    return violations
