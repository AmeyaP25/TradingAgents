"""Automatic buy/sell alerts for the workbench.

A background monitor re-evaluates every tracked holding on an interval. When
the rule-based signal changes to (or away from) an actionable call, or a
user-set price level is crossed, an alert event is written to the store with
the suggested number of shares. Alerts are research notifications; nothing
here places orders.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from tradingagents.client_profile import ClientProfile
from tradingagents.live_market import compute_signal, get_history, get_quote
from tradingagents.workbench_store import AuthError, Store

logger = logging.getLogger(__name__)

ACTIONABLE = {"BUY", "ADD", "TRIM", "SELL"}
NO_SIGNAL = {"UNAVAILABLE", "INSUFFICIENT DATA", None}

ACTION_TEXT = {
    "BUY": "Rules now favour buying",
    "ADD": "Rules now favour adding to your position in",
    "TRIM": "Rules now favour trimming",
    "SELL": "Rules now favour selling",
    "HOLD": "Signal eased to HOLD for",
    "WAIT": "Signal eased to WAIT for",
    "AVOID": "Rules now say avoid buying",
}


class MarketCache:
    """Short-lived cache so the UI and the monitor share Yahoo requests."""

    def __init__(
        self,
        quote_fn: Callable[[str], dict] = get_quote,
        history_fn: Callable[[str, str], dict] = get_history,
        quote_ttl: float = 10.0,
        history_ttl: float = 900.0,
    ):
        self.quote_fn = quote_fn
        self.history_fn = history_fn
        self.quote_ttl = quote_ttl
        self.history_ttl = history_ttl
        self._quotes: dict[str, tuple[float, dict]] = {}
        self._history: dict[tuple[str, str], tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def quote(self, ticker: str) -> dict:
        with self._lock:
            hit = self._quotes.get(ticker)
        if hit and time.monotonic() - hit[0] < self.quote_ttl:
            return hit[1]
        value = self.quote_fn(ticker)
        with self._lock:
            self._quotes[ticker] = (time.monotonic(), value)
        return value

    def history(self, ticker: str, range_key: str = "1y") -> dict:
        key = (ticker, range_key)
        ttl = self.history_ttl if range_key not in {"1d", "5d"} else self.quote_ttl * 3
        with self._lock:
            hit = self._history.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = self.history_fn(ticker, range_key)
        if value.get("available"):
            with self._lock:
                self._history[key] = (time.monotonic(), value)
        return value


def _profile(data: dict | None) -> ClientProfile | None:
    if not data:
        return None
    try:
        return ClientProfile.model_validate(data)
    except Exception:  # noqa: BLE001
        return None


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def portfolio_value(holdings: list[dict], quotes: dict[str, dict]) -> float:
    total = 0.0
    for h in holdings:
        q = quotes.get(h["ticker"]) or {}
        price = q.get("price") if q.get("available") else _num(h.get("cost_basis"))
        total += float(h.get("shares") or 0) * (price or 0.0)
    return total


def build_signal(
    ticker: str,
    *,
    market: MarketCache,
    holdings: list[dict],
    profile_data: dict | None,
    settings: dict,
) -> dict[str, Any]:
    hist = market.history(ticker, "1y")
    if not hist.get("available"):
        return {
            "ticker": ticker,
            "action": "UNAVAILABLE",
            "confidence": "Low",
            "reasons": [f"Price history unavailable: {hist.get('error', 'unknown error')}. No signal produced."],
            "method": "deterministic_rules",
            "trade_suggestion": {"side": None, "shares": 0, "est_value": 0.0, "rationale": "No data.", "assumptions": []},
        }
    tickers = {h["ticker"] for h in holdings} | {ticker}
    quotes = {t: market.quote(t) for t in tickers}
    live = quotes[ticker]
    holding = next((h for h in holdings if h["ticker"] == ticker), None)
    profile = _profile(profile_data)
    result = compute_signal(
        [p["c"] for p in hist["points"]],
        profile=profile,
        holding=holding,
        price=live.get("price") if live.get("available") else None,
        portfolio_value=portfolio_value(holdings, quotes),
        cash=_num(settings.get("cash_available")),
        max_position_pct=_num(settings.get("max_position_pct")),
    )
    result["ticker"] = ticker
    result["profile_applied"] = profile is not None
    result["quote"] = live
    return result


def signal_alert(ticker: str, previous_action: str | None, signal: dict) -> dict | None:
    """Alert event for a signal change, or None when nothing worth telling changed."""
    action = signal.get("action")
    if action in NO_SIGNAL or action == previous_action:
        return None
    if action not in ACTIONABLE and previous_action not in ACTIONABLE:
        return None

    trade = signal.get("trade_suggestion") or {}
    price = (signal.get("indicators") or {}).get("price")
    shares = trade.get("shares") or 0
    title = f"{action} {ticker}"
    if trade.get("side") and shares:
        title += f": {trade['side'].lower()} {shares:g} shares"
    lead = f"{ACTION_TEXT.get(action, 'Signal changed for')} {ticker}"
    reason = (signal.get("reasons") or [""])[0]
    parts = [f"{lead} at about ${price:,.2f}." if price else f"{lead}."]
    if reason:
        parts.append(reason)
    if trade.get("rationale") and trade.get("side"):
        parts.append(trade["rationale"])
    if previous_action:
        parts.append(f"Previous signal: {previous_action}.")
    return {
        "ticker": ticker,
        "kind": "signal",
        "action": action,
        "title": title,
        "message": " ".join(parts),
        "suggested_shares": shares or None,
        "price": price,
        "payload": {
            "previous_action": previous_action,
            "confidence": signal.get("confidence"),
            "levels": signal.get("levels"),
            "trade_suggestion": trade,
            "reasons": (signal.get("reasons") or [])[:3],
        },
    }


def price_alert_hits(alerts: list[dict], quotes: dict[str, dict]) -> list[tuple[dict, dict]]:
    """(alert, event) pairs for active price alerts whose level was crossed."""
    hits = []
    for alert in alerts:
        if not alert.get("active"):
            continue
        q = quotes.get(alert["ticker"]) or {}
        price = q.get("price") if q.get("available") else None
        target = _num(alert.get("price"))
        if price is None or target is None:
            continue
        crossed = price >= target if alert["direction"] == "above" else price <= target
        if not crossed:
            continue
        word = "risen above" if alert["direction"] == "above" else "fallen below"
        note = f" Note: {alert['note']}" if alert.get("note") else ""
        hits.append((alert, {
            "ticker": alert["ticker"],
            "kind": "price",
            "action": None,
            "title": f"{alert['ticker']} has {word} ${target:,.2f}",
            "message": f"{alert['ticker']} is at ${price:,.2f}, crossing your alert level of ${target:,.2f}.{note}",
            "suggested_shares": None,
            "price": price,
            "payload": {"alert_id": alert.get("id"), "direction": alert["direction"], "target": target},
        }))
    return hits


class AlertMonitor:
    def __init__(self, store: Store, market: MarketCache, interval: float = 60.0):
        self.store = store
        self.market = market
        self.interval = interval
        self._token: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self.last_run: float | None = None
        self.last_error: str | None = None

    def set_token(self, token: str | None) -> None:
        self._token = token

    def run_once(self, token: str) -> list[dict]:
        with self._run_lock:
            settings = self.store.get_settings(token)
            if settings.get("alerts_enabled") is False:
                return []
            holdings = self.store.list_holdings(token)
            price_alerts = [a for a in self.store.list_price_alerts(token) if a.get("active")]
            if not holdings and not price_alerts:
                return []
            profile_data = self.store.get_profile(token)
            states = self.store.get_signal_states(token)

            created: list[dict] = []
            for h in holdings:
                ticker = h["ticker"]
                signal = build_signal(ticker, market=self.market, holdings=holdings,
                                      profile_data=profile_data, settings=settings)
                action = signal.get("action")
                if action in NO_SIGNAL:
                    continue
                prev = (states.get(ticker) or {}).get("last_action")
                event = signal_alert(ticker, prev, signal)
                if event:
                    created.append(self.store.add_alert_event(token, event))
                if action != prev:
                    self.store.set_signal_state(token, ticker, action, (signal.get("indicators") or {}).get("price"))

            quotes = {a["ticker"]: self.market.quote(a["ticker"]) for a in price_alerts}
            for alert, event in price_alert_hits(price_alerts, quotes):
                created.append(self.store.add_alert_event(token, event))
                self.store.trigger_price_alert(token, alert["id"])
            self.last_run = time.time()
            self.last_error = None
            return created

    def _loop(self) -> None:
        while not self._stop.wait(5 if self.last_run is None else self.interval):
            token = self._token
            if not token:
                continue
            try:
                self.run_once(token)
            except AuthError:
                self._token = None
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.warning("Alert monitor run failed: %s", exc)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="alert-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
