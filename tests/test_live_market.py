from __future__ import annotations

from datetime import date

import pytest

from tradingagents.client_profile import CashFlowEvent, ClientProfile, ExtractedValue
from tradingagents.alerts import MarketCache
from tradingagents.live_market import (
    compute_signal,
    normalize_ticker,
    risk_band,
    rsi,
    sma,
    suggest_trade,
    years_to_first_withdrawal,
)
from tradingagents.local_api import create_app, signal_payload
from tradingagents.workbench_store import LocalStore

USER, PASSWORD = "lions2-10468483", "d2b633"

TODAY = date(2026, 10, 2)


def _uptrend(n=260, start=100.0, step=0.4):
    return [start + i * step + (1.5 if i % 2 else -1.5) for i in range(n)]


def _downtrend(n=260, start=200.0, step=0.4):
    return [start - i * step + (1.5 if i % 2 else -1.5) for i in range(n)]


def _profile(first_payment_year=2033, risk="Balanced growth and capital protection"):
    return ClientProfile(
        raw_case_study_text="synthetic",
        risk_tolerance=ExtractedValue(value=risk, status="explicit", confidence="high"),
        cash_flows=[CashFlowEvent(event_type="payment", amount=1000, year=first_payment_year, frequency="annual", count=10)],
    )


@pytest.mark.unit
def test_indicators_basic():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 5) is None
    assert rsi([float(i) for i in range(30)]) == 100.0
    assert rsi([1.0] * 5) is None


@pytest.mark.unit
def test_normalize_ticker_rejects_garbage():
    assert normalize_ticker(" aapl ") == "AAPL"
    assert normalize_ticker("brk-b") == "BRK-B"
    with pytest.raises(ValueError):
        normalize_ticker("../etc")


@pytest.mark.unit
def test_insufficient_data_produces_no_signal():
    result = compute_signal([100.0] * 20, today=TODAY)
    assert result["action"] == "INSUFFICIENT DATA"


@pytest.mark.unit
def test_uptrend_not_held_suggests_buy_for_long_horizon_client():
    result = compute_signal(_uptrend(), profile=_profile(2033), today=TODAY)
    assert result["action"] == "BUY"
    assert result["years_to_first_withdrawal"] == 7
    assert any("7 years away" in note for note in result["client_fit"])
    assert result["method"] == "deterministic_rules"


@pytest.mark.unit
def test_downtrend_held_suggests_sell_or_trim():
    result = compute_signal(_downtrend(), holding={"shares": 10, "cost_basis": 150}, today=TODAY)
    assert result["action"] in {"SELL", "TRIM"}
    assert result["position"]["shares"] == 10


@pytest.mark.unit
def test_stop_loss_breach_forces_sell():
    closes = _uptrend()
    result = compute_signal(closes, holding={"shares": 5, "cost_basis": closes[-1] * 1.5}, today=TODAY)
    assert result["action"] == "SELL"
    assert "stop-loss" in result["reasons"][0]


@pytest.mark.unit
def test_near_withdrawal_blocks_buy_even_in_uptrend():
    result = compute_signal(_uptrend(), profile=_profile(2027), today=TODAY)
    assert result["action"] != "BUY"
    assert any("protect" in r.lower() for r in result["reasons"])


@pytest.mark.unit
def test_stop_loss_default_is_labelled_assumption_and_override_respected():
    default = compute_signal(_uptrend(), profile=_profile(risk="conservative investor"), today=TODAY)
    assert default["levels"]["stop_loss_pct"] == 0.10
    assert any("assumption" in a for a in default["assumptions"])

    custom = compute_signal(_uptrend(), holding={"shares": 1, "cost_basis": 100, "stop_loss_pct": 0.3}, today=TODAY)
    assert custom["levels"]["stop_loss_pct"] == 0.3
    assert custom["levels"]["stop_loss_price"] == 70.0


@pytest.mark.unit
def test_profile_helpers():
    assert risk_band(None) == ("balanced", False)
    assert risk_band(_profile(risk="aggressive growth")) == ("aggressive", True)
    assert years_to_first_withdrawal(_profile(2033), TODAY) == 7
    assert years_to_first_withdrawal(None, TODAY) is None


def _market(closes, price=None):
    return MarketCache(
        quote_fn=lambda t: {"ticker": t, "available": True, "price": price if price is not None else closes[-1]},
        history_fn=lambda t, r: {"available": True, "points": [{"t": str(i), "c": c} for i, c in enumerate(closes)]},
    )


def _signed_in(tmp_path):
    store = LocalStore(tmp_path)
    return store, store.login(USER, PASSWORD, False)["token"]


@pytest.mark.unit
def test_signal_payload_uses_store_profile_holding_and_cash(tmp_path):
    store, tok = _signed_in(tmp_path)
    store.save_profile(tok, _profile().model_dump())
    store.upsert_holding(tok, {"ticker": "NVDA", "shares": 2, "cost_basis": 100})
    store.save_settings(tok, {"cash_available": 50000})
    closes = _uptrend()

    result = signal_payload(store, tok, "NVDA", _market(closes))
    assert result["profile_applied"] is True
    assert result["held"] is True
    assert result["action"] in {"ADD", "HOLD"}
    assert "trade_suggestion" in result


@pytest.mark.unit
def test_signal_payload_reports_unavailable_instead_of_guessing(tmp_path):
    store, tok = _signed_in(tmp_path)
    market = MarketCache(
        quote_fn=lambda t: {"available": False},
        history_fn=lambda t, r: {"available": False, "error": "offline", "points": []},
    )
    assert signal_payload(store, tok, "NVDA", market)["action"] == "UNAVAILABLE"


@pytest.mark.unit
def test_suggest_trade_sizes_buys_and_sells():
    sell = suggest_trade("SELL", price=50, shares_held=12, portfolio_value=600, cash=0, max_position_pct=0.1)
    assert (sell["side"], sell["shares"], sell["est_value"]) == ("SELL", 12, 600)

    trim = suggest_trade("TRIM", price=50, shares_held=12, portfolio_value=600, cash=0, max_position_pct=0.1)
    assert (trim["side"], trim["shares"]) == ("SELL", 4)

    buy = suggest_trade("BUY", price=100, shares_held=0, portfolio_value=0, cash=20000, max_position_pct=0.1)
    assert (buy["side"], buy["shares"]) == ("BUY", 20)

    capped = suggest_trade("BUY", price=100, shares_held=0, portfolio_value=90000, cash=500, max_position_pct=0.1)
    assert capped["shares"] == 5

    full = suggest_trade("ADD", price=100, shares_held=100, portfolio_value=10000, cash=0, max_position_pct=0.1)
    assert full["shares"] == 0 and "target size" in full["rationale"]

    unsized = suggest_trade("BUY", price=100, shares_held=0, portfolio_value=0, cash=None, max_position_pct=0.1)
    assert unsized["shares"] == 0 and "cash" in unsized["rationale"].lower()

    assert suggest_trade("HOLD", price=100, shares_held=5, portfolio_value=500, cash=0, max_position_pct=0.1)["side"] is None


@pytest.mark.unit
def test_add_becomes_hold_when_position_is_already_full_size():
    closes = _uptrend()
    full = compute_signal(closes, holding={"shares": 100, "cost_basis": 100}, portfolio_value=100 * closes[-1],
                          cash=0, today=TODAY)
    assert full["action"] == "HOLD"
    assert full["trade_suggestion"]["side"] is None
    assert any("maximum size" in r for r in full["reasons"])

    room = compute_signal(closes, holding={"shares": 1, "cost_basis": 100}, portfolio_value=closes[-1],
                          cash=100000, today=TODAY)
    assert room["action"] == "ADD" and room["trade_suggestion"]["shares"] > 0


@pytest.mark.unit
def test_live_routes_registered(tmp_path):
    paths = {getattr(r, "path", None) for r in create_app(LocalStore(tmp_path), start_monitor=False).routes}
    for expected in (
        "/api/quotes", "/api/history/{ticker}", "/api/signal/{ticker}", "/api/holdings", "/api/profile",
        "/login", "/auth/login", "/auth/logout", "/api/alerts", "/api/alerts/check", "/api/price-alerts",
        "/api/settings",
    ):
        assert expected in paths
