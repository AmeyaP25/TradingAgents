from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from starlette.requests import Request

from tradingagents.alerts import AlertMonitor, MarketCache, price_alert_hits, signal_alert
from tradingagents.local_api import LoginRequest, SESSION_COOKIE, create_app
from tradingagents.workbench_store import AuthError, LocalStore, StoreError, SupabaseStore, make_store

USER, PASSWORD = "lions2-10468483", "d2b633"


def _uptrend(n=260):
    return [100 + i * 0.4 + (1.5 if i % 2 else -1.5) for i in range(n)]


def _downtrend(n=260):
    return [200 - i * 0.4 + (1.5 if i % 2 else -1.5) for i in range(n)]


class FakeMarket(MarketCache):
    def __init__(self, closes_by_ticker, prices=None):
        self.series = closes_by_ticker
        self.prices = prices or {}
        super().__init__(quote_fn=self._quote, history_fn=self._history, quote_ttl=0, history_ttl=0)

    def _quote(self, t):
        if t not in self.series and t not in self.prices:
            return {"ticker": t, "available": False}
        return {"ticker": t, "available": True, "price": self.prices.get(t, self.series.get(t, [0])[-1])}

    def _history(self, t, r):
        closes = self.series.get(t)
        if not closes:
            return {"available": False, "error": "none", "points": []}
        return {"available": True, "points": [{"t": str(i), "c": c} for i, c in enumerate(closes)]}


# ---------------------------------------------------------------- auth

@pytest.mark.unit
def test_only_the_issued_account_can_sign_in(tmp_path):
    store = LocalStore(tmp_path)
    assert store.login(USER, "wrong", False) == {"ok": False, "error": "invalid_credentials"}
    assert store.login("someone-else", PASSWORD, False)["ok"] is False
    ok = store.login(USER, PASSWORD, False)
    assert ok["ok"] is True and len(ok["token"]) == 64
    assert store.session(ok["token"])["username"] == USER


@pytest.mark.unit
def test_password_is_not_stored_in_plaintext():
    from pathlib import Path

    import tradingagents.workbench_store as ws

    assert PASSWORD not in Path(ws.__file__).read_text(encoding="utf-8")


@pytest.mark.unit
def test_remember_me_extends_session_lifetime(tmp_path):
    store = LocalStore(tmp_path)
    short = store.login(USER, PASSWORD, False)
    long = store.login(USER, PASSWORD, True)
    now = datetime.now(timezone.utc)
    short_hours = (datetime.fromisoformat(short["expires_at"]) - now).total_seconds() / 3600
    long_days = (datetime.fromisoformat(long["expires_at"]) - now).total_seconds() / 86400
    assert 11 < short_hours <= 12
    assert 29 < long_days <= 30
    assert long["remember"] is True


@pytest.mark.unit
def test_lockout_after_repeated_failures(tmp_path):
    store = LocalStore(tmp_path)
    for _ in range(5):
        store.login(USER, "bad", False)
    assert store.login(USER, PASSWORD, False)["error"] == "account_locked"


@pytest.mark.unit
def test_data_requires_a_live_session(tmp_path):
    store = LocalStore(tmp_path)
    with pytest.raises(AuthError):
        store.list_holdings("forged")
    tok = store.login(USER, PASSWORD, False)["token"]
    store.upsert_holding(tok, {"ticker": "AAPL", "shares": 1, "cost_basis": 10})
    store.logout(tok)
    assert store.session(tok) is None
    with pytest.raises(AuthError):
        store.list_holdings(tok)


@pytest.mark.unit
def test_login_endpoint_sets_persistent_cookie_only_with_remember(tmp_path):
    app = create_app(LocalStore(tmp_path), start_monitor=False)
    login = next(r for r in app.routes if getattr(r, "path", None) == "/auth/login").endpoint
    request = Request({"type": "http", "scheme": "http", "headers": [], "path": "/auth/login", "method": "POST"})

    remembered = login(LoginRequest(username=USER, password=PASSWORD, remember=True), request)
    cookie = remembered.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=") and "Max-Age=2592000" in cookie and "HttpOnly" in cookie

    session_only = login(LoginRequest(username=USER, password=PASSWORD, remember=False), request)
    assert "Max-Age" not in session_only.headers["set-cookie"]

    denied = login(LoginRequest(username=USER, password="nope", remember=False), request)
    assert denied.status_code == 401 and "set-cookie" not in denied.headers


@pytest.mark.unit
def test_make_store_picks_local_without_supabase_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    assert make_store(tmp_path).backend == "local"
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_x")
    assert make_store(tmp_path).backend == "supabase"


# ---------------------------------------------------------------- supabase client

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        return self._body


class _Http:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, headers, json, timeout):
        self.calls.append((url, headers, json))
        return self.resp


@pytest.mark.unit
def test_supabase_store_calls_rpc_with_publishable_key():
    http = _Http(_Resp(200, [{"ticker": "AAPL"}]))
    store = SupabaseStore("https://p.supabase.co/", "sb_publishable_abc", http=http)
    assert store.list_holdings("tok") == [{"ticker": "AAPL"}]
    url, headers, body = http.calls[0]
    assert url == "https://p.supabase.co/rest/v1/rpc/wins_list_holdings"
    assert headers["apikey"] == "sb_publishable_abc" and "Authorization" not in headers
    assert body == {"p_token": "tok"}


@pytest.mark.unit
def test_supabase_store_maps_errors():
    expired = SupabaseStore("https://p", "k", http=_Http(_Resp(400, {"code": "28000", "message": "invalid_session"})))
    with pytest.raises(AuthError):
        expired.get_settings("tok")
    broken = SupabaseStore("https://p", "k", http=_Http(_Resp(500, {"message": "boom"})))
    with pytest.raises(StoreError):
        broken.get_settings("tok")


# ---------------------------------------------------------------- alerts

@pytest.mark.unit
def test_signal_alert_only_fires_on_meaningful_changes():
    sell = {"action": "SELL", "indicators": {"price": 90.0}, "reasons": ["Below trend."],
            "trade_suggestion": {"side": "SELL", "shares": 10, "rationale": "Sell all 10 shares."}}
    event = signal_alert("AAPL", "HOLD", sell)
    assert event["title"] == "SELL AAPL: sell 10 shares"
    assert event["suggested_shares"] == 10 and "Previous signal: HOLD" in event["message"]

    assert signal_alert("AAPL", "SELL", sell) is None
    assert signal_alert("AAPL", None, {"action": "HOLD"}) is None
    assert signal_alert("AAPL", "HOLD", {"action": "WAIT"}) is None
    assert signal_alert("AAPL", "HOLD", {"action": "UNAVAILABLE"}) is None
    assert signal_alert("AAPL", "SELL", {"action": "HOLD", "indicators": {"price": 1}})["action"] == "HOLD"


@pytest.mark.unit
def test_price_alert_hits():
    alerts = [
        {"id": 1, "ticker": "AAPL", "direction": "below", "price": 100, "active": True},
        {"id": 2, "ticker": "AAPL", "direction": "above", "price": 200, "active": True},
        {"id": 3, "ticker": "AAPL", "direction": "below", "price": 150, "active": False},
    ]
    hits = price_alert_hits(alerts, {"AAPL": {"available": True, "price": 95}})
    assert [a["id"] for a, _ in hits] == [1]
    assert "fallen below" in hits[0][1]["title"]
    assert price_alert_hits(alerts, {"AAPL": {"available": False}}) == []


@pytest.mark.unit
def test_monitor_creates_one_alert_per_change_and_triggers_price_alerts(tmp_path):
    store = LocalStore(tmp_path)
    tok = store.login(USER, PASSWORD, True)["token"]
    store.upsert_holding(tok, {"ticker": "DOWN", "shares": 30, "cost_basis": 150})
    store.upsert_holding(tok, {"ticker": "UP", "shares": 0})
    store.save_settings(tok, {"cash_available": 10000})
    store.add_price_alert(tok, {"ticker": "UP", "direction": "above", "price": 150, "note": ""})

    market = FakeMarket({"DOWN": _downtrend(), "UP": _uptrend()})
    monitor = AlertMonitor(store, market)

    first = monitor.run_once(tok)
    kinds = sorted((e["kind"], e["ticker"], e.get("action")) for e in first)
    assert ("signal", "DOWN", "SELL") in kinds
    assert ("signal", "UP", "BUY") in kinds
    assert ("price", "UP", None) in kinds
    down = next(e for e in first if e["ticker"] == "DOWN" and e["kind"] == "signal")
    assert down["suggested_shares"] == 30
    up = next(e for e in first if e["ticker"] == "UP" and e["kind"] == "signal")
    assert up["suggested_shares"] and up["suggested_shares"] > 0

    assert monitor.run_once(tok) == []
    assert store.list_price_alerts(tok)[0]["active"] is False
    assert len(store.list_alert_events(tok)) == 3

    store.mark_alerts_read(tok, None)
    assert store.list_alert_events(tok, unread_only=True) == []


@pytest.mark.unit
def test_monitor_respects_disabled_alerts(tmp_path):
    store = LocalStore(tmp_path)
    tok = store.login(USER, PASSWORD, False)["token"]
    store.upsert_holding(tok, {"ticker": "DOWN", "shares": 5, "cost_basis": 150})
    store.save_settings(tok, {"alerts_enabled": False})
    assert AlertMonitor(store, FakeMarket({"DOWN": _downtrend()})).run_once(tok) == []
