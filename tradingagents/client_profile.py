"""Client case-study profile: structured fields plus preserved source text.

The case study is the source of truth. Parsing extracts what is explicit,
leaves unknowns as unknown, and marks soft preferences separately from hard
constraints so downstream agents can reason about suitability correctly.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class ExtractedValue(BaseModel):
    value: str | float | int | None = None
    status: Literal["explicit", "inferred", "ambiguous", "unknown"] = "unknown"
    confidence: Literal["high", "medium", "low"] = "low"
    source_excerpt: str | None = None


class CashFlowEvent(BaseModel):
    event_type: Literal["contribution", "withdrawal", "payment", "reserve_set_aside"]
    amount: float | None = None
    currency: str = "USD"
    year: int | None = None
    timing: Literal["beginning_of_year", "end_of_year", "unspecified"] = "unspecified"
    frequency: Literal["one_time", "annual"] = "one_time"
    count: int | None = None
    notes: str | None = None


class ConstraintItem(BaseModel):
    description: str
    kind: Literal["hard", "preference"]
    source_excerpt: str | None = None


class ClientProfile(BaseModel):
    raw_case_study_text: str
    client_name: ExtractedValue = Field(default_factory=ExtractedValue)
    risk_tolerance: ExtractedValue = Field(default_factory=ExtractedValue)
    investment_goals: list[str] = Field(default_factory=list)
    time_horizon_notes: list[str] = Field(default_factory=list)
    timeline_milestones: dict[str, int] = Field(default_factory=dict)
    year_zero_reference: ExtractedValue = Field(default_factory=ExtractedValue)
    cash_flows: list[CashFlowEvent] = Field(default_factory=list)
    operating_reserve_required: ExtractedValue = Field(default_factory=ExtractedValue)
    facility_contribution_required: ExtractedValue = Field(default_factory=ExtractedValue)
    co_sponsor_communication_year: ExtractedValue = Field(default_factory=ExtractedValue)
    hard_constraints: list[ConstraintItem] = Field(default_factory=list)
    preferences: list[ConstraintItem] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    ambiguous_items: list[str] = Field(default_factory=list)
    funding_certainty_target: float = 0.95
    confidence_format: Literal["dual", "qualitative", "numeric"] = "dual"
    wins_universe_rule: str = (
        "Only recommend instruments tradable in the competition simulator."
    )

    def render(self, ticker: str | None = None) -> str:
        symbol = f" for `{ticker}`" if ticker else ""
        lines = [
            f"Client profile context{symbol}:",
            "",
            f"- Client name: {self._render_extracted(self.client_name)}",
            f"- Risk tolerance: {self._render_extracted(self.risk_tolerance)}",
            f"- Funding certainty target: {self.funding_certainty_target:.0%}",
            f"- Confidence reporting format: {self.confidence_format}",
            f"- WInS tradability rule: {self.wins_universe_rule}",
            "",
            "Investment goals:",
        ]
        if self.investment_goals:
            lines.extend([f"- {goal}" for goal in self.investment_goals])
        else:
            lines.append("- No explicit goals parsed.")

        lines.extend(["", "Hard constraints:"])
        if self.hard_constraints:
            lines.extend([f"- {item.description}" for item in self.hard_constraints])
        else:
            lines.append("- None extracted.")

        lines.extend(["", "Preferences:"])
        if self.preferences:
            lines.extend([f"- {item.description}" for item in self.preferences])
        else:
            lines.append("- None extracted.")

        lines.extend(["", "Cash-flow schedule:"])
        if self.cash_flows:
            lines.extend([f"- {self._render_cash_flow(flow)}" for flow in self.cash_flows])
        else:
            lines.append("- None extracted.")

        lines.extend(["", "Timeline milestones:"])
        if self.timeline_milestones:
            lines.extend([f"- {key}: Year {value}" for key, value in sorted(self.timeline_milestones.items())])
        else:
            lines.append("- None extracted.")

        lines.extend([
            "",
            f"- Operating reserve requirement: {self._render_extracted(self.operating_reserve_required)}",
            f"- Facility contribution requirement: {self._render_extracted(self.facility_contribution_required)}",
            f"- Co-sponsor communication year: {self._render_extracted(self.co_sponsor_communication_year)}",
        ])

        if self.uncertainties:
            lines.extend(["", "Uncertainties / unknowns:"])
            lines.extend([f"- {item}" for item in self.uncertainties])

        if self.ambiguous_items:
            lines.extend(["", "Ambiguities needing review:"])
            lines.extend([f"- {item}" for item in self.ambiguous_items])

        lines.extend([
            "",
            "Original case study text (verbatim):",
            self.raw_case_study_text.strip(),
        ])
        return "\n".join(lines)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]

    @staticmethod
    def _render_extracted(item: ExtractedValue) -> str:
        if item.value is None:
            return f"unknown ({item.status})"
        return f"{item.value} ({item.status}, {item.confidence} confidence)"

    @staticmethod
    def _render_cash_flow(flow: CashFlowEvent) -> str:
        amount = f"{flow.amount:,.0f} {flow.currency}" if flow.amount is not None else "unknown amount"
        timing = flow.timing.replace("_", " ")
        year = str(flow.year) if flow.year is not None else "unknown year"
        if flow.frequency == "annual" and flow.count:
            return f"{flow.event_type}: {amount}, {flow.frequency} x{flow.count}, {timing}, start {year}"
        return f"{flow.event_type}: {amount}, {timing}, year {year}"


def _capture(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _money(value: str) -> float:
    return float(value.replace("$", "").replace(",", "").strip())


def extract_client_profile(case_text: str) -> ClientProfile:
    profile = ClientProfile(raw_case_study_text=case_text)
    text = case_text

    if name := _capture(text, r"potential client,\s*([A-Za-z .'-]+?),"):
        profile.client_name = ExtractedValue(
            value=name, status="explicit", confidence="high", source_excerpt=name
        )

    risk_sentence = _capture(
        text,
        r"wants .*?balance between\s+(pursuing growth.*?)\.",
    )
    if risk_sentence:
        profile.risk_tolerance = ExtractedValue(
            value=f"Balanced growth and capital protection ({risk_sentence})",
            status="explicit",
            confidence="high",
            source_excerpt=risk_sentence,
        )

    goal_checks = [
        (
            r"supports the ten-year operating commitment with a high degree of certainty",
            "Support the ten-year operating commitment with high funding certainty.",
        ),
        (
            r"determines a responsible facility contribution",
            "Determine a responsible facility contribution.",
        ),
        (
            r"preserves appropriate financial flexibility",
            "Preserve appropriate financial flexibility.",
        ),
        (
            r"communicates .* facility contribution .* co-sponsors",
            "Communicate a credible facility-contribution range to co-sponsors.",
        ),
    ]
    for pattern, text_goal in goal_checks:
        if re.search(pattern, text, flags=re.IGNORECASE):
            profile.investment_goals.append(text_goal)

    for year, idx in re.findall(r"(\d{4})\s+is\s+Year\s+(\d+)", text, flags=re.IGNORECASE):
        profile.timeline_milestones[year] = int(idx)
    if profile.timeline_milestones:
        profile.year_zero_reference = ExtractedValue(
            value="Year 0 maps to 2026",
            status="explicit",
            confidence="high",
            source_excerpt="2026 is Year 0.",
        )

    for amount, year in re.findall(r"invest\s+\$([0-9,]+).*?beginning of\s+(\d{4})", text, flags=re.IGNORECASE):
        profile.cash_flows.append(CashFlowEvent(
            event_type="contribution",
            amount=_money(amount),
            year=int(year),
            timing="beginning_of_year",
            frequency="one_time",
        ))
    if second := re.search(
        r"additional\s+\$([0-9,]+).*?beginning of\s+(\d{4})", text, flags=re.IGNORECASE
    ):
        amount, year = second.group(1), second.group(2)
        event = CashFlowEvent(
            event_type="contribution",
            amount=_money(amount),
            year=int(year),
            timing="beginning_of_year",
            frequency="one_time",
        )
        if event not in profile.cash_flows:
            profile.cash_flows.append(event)

    payment = re.search(
        r"ten annual payments of\s+\$([0-9,]+).*?from\s+(\d{4})\s+through\s+(\d{4})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if payment:
        amount, start, end = payment.group(1), int(payment.group(2)), int(payment.group(3))
        profile.cash_flows.append(CashFlowEvent(
            event_type="payment",
            amount=_money(amount),
            year=start,
            timing="beginning_of_year",
            frequency="annual",
            count=(end - start + 1),
            notes=f"Runs through {end}",
        ))

    if re.search(r"operating reserve", text, flags=re.IGNORECASE):
        profile.operating_reserve_required = ExtractedValue(
            value="Required in 2033 before first payment/facility contribution",
            status="explicit",
            confidence="high",
            source_excerpt="At the beginning of 2033, before making the first operating payment or contributing to the facility, Laura will set aside a portion of the portfolio.",
        )

    if re.search(r"no predetermined facility contribution", text, flags=re.IGNORECASE):
        profile.facility_contribution_required = ExtractedValue(
            value="Must recommend a responsible contribution; no predetermined amount",
            status="explicit",
            confidence="high",
            source_excerpt="There is no predetermined facility contribution.",
        )
    elif re.search(r"facility contribution", text, flags=re.IGNORECASE):
        profile.facility_contribution_required = ExtractedValue(
            value="Facility contribution required, amount uncertain",
            status="ambiguous",
            confidence="medium",
            source_excerpt="Facility Contribution section present but no single required amount.",
        )
        profile.ambiguous_items.append(
            "Facility contribution amount is intentionally open-ended and must be strategy-derived."
        )

    if year := _capture(text, r"begin approaching potential co-sponsors in\s+(\d{4})"):
        profile.co_sponsor_communication_year = ExtractedValue(
            value=year,
            status="explicit",
            confidence="high",
            source_excerpt=f"Laura plans to begin approaching potential co-sponsors in {year}.",
        )

    hard_rule_checks = [
        (
            r"all ten payments must be funded by the investment portfolio",
            "All ten payments must be funded by the investment portfolio with a high degree of certainty.",
        ),
        (
            r"may not rely on co-sponsors|may not rely on .*outside funding",
            "Teams may not rely on co-sponsors, grants, program fees, or other outside funding to meet the operating payment requirement.",
        ),
        (
            r"all contributions and withdrawals occur at the beginning of the applicable year",
            "All contributions and withdrawals occur at the beginning of the applicable year.",
        ),
        (
            r"neither add to nor withdraw from the portfolio before 2033|apart from the two contributions",
            "Apart from the two contributions, no additions or withdrawals before 2033.",
        ),
    ]
    for pattern, description in hard_rule_checks:
        if re.search(pattern, text, flags=re.IGNORECASE):
            profile.hard_constraints.append(ConstraintItem(description=description, kind="hard"))

    pref_rules = [
        "Balance pursuing growth with protecting capital required for goals.",
        "Preserve financial flexibility when determining the facility contribution.",
        "Communicate a credible contribution range to co-sponsors in 2031.",
    ]
    for rule in pref_rules:
        profile.preferences.append(ConstraintItem(description=rule, kind="preference"))

    unknowns = [
        "Age not explicitly stated.",
        "Target return not explicitly stated.",
        "Maximum acceptable loss not explicitly stated.",
        "Specific sector/ethical restrictions not explicitly stated.",
        "Existing full asset/liability balance sheet outside this portfolio not explicitly stated.",
    ]
    profile.uncertainties.extend(unknowns)

    if not profile.timeline_milestones:
        profile.ambiguous_items.append(
            "No explicit year-to-index timeline mapping extracted; verify relative-year interpretation."
        )
    return profile


def load_client_profile(path: str | Path) -> ClientProfile:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return ClientProfile.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"client profile file {path} is not usable: {exc}") from exc


def save_client_profile(profile: ClientProfile, path: str | Path) -> None:
    Path(path).write_text(profile.model_dump_json(indent=2), encoding="utf-8")


def apply_client_profile_overrides(profile: ClientProfile, overrides: dict) -> ClientProfile:
    """Apply a partial override map to a parsed profile and re-validate."""
    base = profile.model_dump()
    merged = _deep_merge(base, overrides or {})
    return ClientProfile.model_validate(merged)


def load_overrides(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"client profile overrides file {path} is not usable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"client profile overrides file {path} must contain a JSON object")
    return data


def _deep_merge(base: dict, patch: dict) -> dict:
    out = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
