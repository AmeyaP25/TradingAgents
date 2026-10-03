from __future__ import annotations

import pytest
from fastapi import HTTPException

from tradingagents.local_api import AnalyzeRequest, analyze_payload, create_app, extract_profile_payload


@pytest.mark.unit
def test_health_endpoint():
    app = create_app()
    health = [route for route in app.routes if getattr(route, "path", None) == "/health"][0]
    assert health.endpoint() == {"status": "ok"}


@pytest.mark.unit
def test_extract_profile_endpoint_returns_structured_profile():
    body = extract_profile_payload(
        "Potential client, Laura Gao, plans to invest $300,000 at the beginning of 2027."
    )
    assert body["client_name"]["value"] == "Laura Gao"
    assert body["cash_flows"][0]["amount"] == 300000.0


@pytest.mark.unit
def test_analyze_rejects_conflicting_profile_inputs():
    with pytest.raises(HTTPException) as exc:
        analyze_payload(
            AnalyzeRequest(
                ticker="NVDA",
                trade_date="2026-01-10",
                client_profile={"raw_case_study_text": "x"},
                case_study_text="y",
            )
        )
    assert exc.value.status_code == 400
    assert "either client_profile or case_study_text" in exc.value.detail
