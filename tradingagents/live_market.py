"""Live quotes, price history, and rule-based buy/hold/sell timing signals.

Signals here are deterministic technical rules adjusted by the client profile.
They are research guidance, not AI analysis and not trade instructions.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any

from tradingagents.client_profile import ClientProfile
from tradingagents.workbench_store import normalize_ticker

DISCLAIMER = (
    "Rule-based research signal from delayed public market data. It is not a "
    "guarantee of profit and does not execute trades; the human makes the final decision."
)

HISTORY_RANGES: dict[str, tuple[str, str]] = {
    "1d": ("1d", "5m"),
    "5d": ("5d", "30m"),
    "1mo": ("1mo", "1d"),
    "6mo": ("6mo", "1d"),
    "1y": ("1y", "1d"),
    "5y": ("5y", "1wk"),
}

STOP_LOSS_BY_RISK = {"conservative": 0.10, "balanced": 0.15, "aggressive": 0.25}
MAX_POSITION_BY_RISK = {"conservative": 0.05, "balanced": 0.10, "aggressive": 0.15}


def _clean(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Market data (Yahoo Finance)
# ---------------------------------------------------------------------------

def get_quote(ticker: str) -> dict[str, Any]:
    """Latest quote for one ticker; ``available`` is False when Yahoo has no answer."""
    from tradingagents.dataflows.vendors.yahoo.live import fetch_fast_quote

    symbol = normalize_ticker(ticker)
    try:
        raw = fetch_fast_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        return {"ticker": symbol, "available": False, "error": str(exc), "as_of": _now_iso()}
    data = (
        {k: (v if k == "currency" else _clean(v)) for k, v in raw.items()} if raw else None
    )
    if not data or data.get("price") is None:
        return {"ticker": symbol, "available": False, "error": "No quote returned", "as_of": _now_iso()}

    prev = data.get("previous_close")
    change = data["price"] - prev if prev else None
    return {
        "ticker": symbol,
        "available": True,
        **data,
        "change": change,
        "change_pct": (change / prev * 100) if change is not None and prev else None,
        "as_of": _now_iso(),
        "source": "Yahoo Finance (quotes may be delayed)",
    }


def get_history(ticker: str, range_key: str = "6mo") -> dict[str, Any]:
    """Close-price series for charting and signals."""
    from tradingagents.dataflows.vendors.yahoo.live import fetch_close_history

    symbol = normalize_ticker(ticker)
    if range_key not in HISTORY_RANGES:
        raise ValueError(f"Unsupported range {range_key!r}; use one of {sorted(HISTORY_RANGES)}")
    period, interval = HISTORY_RANGES[range_key]

    try:
        rows = fetch_close_history(symbol, period, interval)
    except Exception as exc:  # noqa: BLE001
        return {"ticker": symbol, "range": range_key, "available": False, "error": str(exc), "points": []}
    if not rows:
        return {"ticker": symbol, "range": range_key, "available": False, "error": "No price history", "points": []}

    points = []
    for ts, close in rows:
        value = _clean(close)
        if value is not None:
            points.append({"t": ts.isoformat(), "c": round(value, 4)})
    return {
        "ticker": symbol,
        "range": range_key,
        "interval": interval,
        "available": bool(points),
        "points": points,
        "source": "Yahoo Finance",
    }


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(values) <= period:
        return None
    gains, losses = [], []
    for prev, cur in zip(values[:-1], values[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


# ---------------------------------------------------------------------------
# Client-profile context for signals
# ---------------------------------------------------------------------------

def risk_band(profile: ClientProfile | None) -> tuple[str, bool]:
    """Return (band, inferred_from_profile)."""
    if profile is None or profile.risk_tolerance.value is None:
        return "balanced", False
    text = str(profile.risk_tolerance.value).lower()
    if re.search(r"conservative|low risk|preserv|capital protection only", text):
        return "conservative", True
    if re.search(r"aggressive|high risk|maximi[sz]e growth|speculative", text):
        return "aggressive", True
    if re.search(r"balanc|moderate|growth and capital protection", text):
        return "balanced", True
    return "balanced", False


def years_to_first_withdrawal(profile: ClientProfile | None, today: date) -> int | None:
    if profile is None:
        return None
    years = [
        cf.year for cf in profile.cash_flows
        if cf.year is not None and cf.event_type in {"payment", "withdrawal"}
    ]
    if not years:
        return None
    return max(min(years) - today.year, 0)


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

def compute_signal(
    closes: list[float],
    *,
    profile: ClientProfile | None = None,
    holding: dict[str, Any] | None = None,
    price: float | None = None,
    today: date | None = None,
    portfolio_value: float | None = None,
    cash: float | None = None,
    max_position_pct: float | None = None,
) -> dict[str, Any]:
    """Deterministic buy/hold/sell timing signal from daily closes.

    ``closes`` should be about a year of daily closes, oldest first.
    """
    today = today or date.today()
    closes = [c for c in (_clean(x) for x in closes) if c is not None]
    held = bool(holding and (holding.get("shares") or 0) > 0)
    base: dict[str, Any] = {
        "method": "deterministic_rules",
        "disclaimer": DISCLAIMER,
        "generated_at": _now_iso(),
        "held": held,
    }
    if len(closes) < 50:
        return {
            **base,
            "action": "INSUFFICIENT DATA",
            "confidence": "Low",
            "score": 0,
            "rule_agreement": None,
            "indicators": {},
            "levels": {},
            "position": None,
            "reasons": ["Fewer than 50 daily closes available; no signal is produced rather than guessing."],
            "client_fit": [],
            "assumptions": [],
            "trade_suggestion": {"side": None, "shares": 0, "est_value": 0.0,
                                 "rationale": "No trade suggested without enough data.", "assumptions": []},
        }

    last = price if price is not None else closes[-1]
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    rsi14 = rsi(closes, 14)
    high_52w = max(closes[-252:])
    drawdown = (last / high_52w - 1) * 100
    ret_3m = (last / closes[-63] - 1) * 100 if len(closes) >= 63 else None

    band, band_from_profile = risk_band(profile)
    horizon = years_to_first_withdrawal(profile, today)
    long_horizon = horizon is not None and horizon >= 5
    near_withdrawal = horizon is not None and horizon <= 2

    reasons: list[str] = []
    client_fit: list[str] = []
    assumptions: list[str] = []
    votes: list[int] = []

    def vote(points: int, reason: str) -> None:
        votes.append(points)
        reasons.append(reason)

    if sma200 is not None:
        if last > sma200:
            vote(1, f"Price {last:.2f} is above its 200-day average {sma200:.2f} (long-term uptrend).")
        else:
            vote(-1, f"Price {last:.2f} is below its 200-day average {sma200:.2f} (long-term downtrend).")
        if sma50 > sma200:
            vote(1, "50-day average is above the 200-day average (bullish trend structure).")
        else:
            vote(-1, "50-day average is below the 200-day average (bearish trend structure).")
    else:
        assumptions.append("Under 200 days of history; long-term trend rules skipped.")
        if last > sma50:
            vote(1, f"Price is above its 50-day average {sma50:.2f}.")
        else:
            vote(-1, f"Price is below its 50-day average {sma50:.2f}.")

    if ret_3m is not None:
        if ret_3m > 5:
            vote(1, f"3-month momentum is positive ({ret_3m:+.1f}%).")
        elif ret_3m < -10:
            vote(-1, f"3-month momentum is weak ({ret_3m:+.1f}%).")

    if rsi14 is not None:
        overbought = 80 if long_horizon else 70
        if rsi14 >= overbought:
            vote(-1, f"RSI {rsi14:.0f} is overbought; better entries are usually after a pullback.")
        elif rsi14 <= 30:
            vote(1, f"RSI {rsi14:.0f} is oversold; potential entry if the trend holds.")

    if band == "conservative" and drawdown < -20:
        vote(-1, f"Down {abs(drawdown):.0f}% from its 52-week high, beyond a conservative client's comfort.")

    if near_withdrawal:
        vote(-1, "Client withdrawals start within ~2 years; bias toward protecting capital.")
        client_fit.append(
            f"First scheduled withdrawal is about {horizon} year(s) away, so short-term "
            "volatility directly threatens funding. Equity exposure should be reduced, not added."
        )
    elif long_horizon:
        client_fit.append(
            f"First scheduled withdrawal is about {horizon} years away, so the signal weights the "
            "long-term trend and tolerates short-term overbought readings."
        )
    elif horizon is not None:
        client_fit.append(f"First scheduled withdrawal is about {horizon} years away (medium horizon).")
    else:
        client_fit.append("No withdrawal schedule in the client profile; horizon treated as unknown.")

    if band_from_profile:
        client_fit.append(f"Risk tolerance read from the profile as '{band}'.")
    else:
        assumptions.append("Risk tolerance not clearly stated; 'balanced' assumed.")

    stop_pct = None
    if holding and holding.get("stop_loss_pct") is not None:
        stop_pct = float(holding["stop_loss_pct"])
        client_fit.append(f"Using your stop-loss of {stop_pct * 100:.0f}% set on this holding.")
    else:
        stop_pct = STOP_LOSS_BY_RISK[band]
        assumptions.append(
            f"Case does not state a maximum acceptable loss; a {stop_pct * 100:.0f}% stop-loss "
            f"for a '{band}' client is an assumption you can override per holding."
        )

    if profile is not None and profile.hard_constraints:
        client_fit.append(
            f"{len(profile.hard_constraints)} hard constraint(s) in the profile still apply; "
            "run the full AI analysis to check this position against them."
        )

    score = sum(votes)
    position = None
    stop_breached = False
    cost = _clean(holding.get("cost_basis")) if holding else None
    if held:
        shares = float(holding["shares"])
        market_value = shares * last
        position = {
            "shares": shares,
            "cost_basis": cost,
            "market_value": market_value,
            "unrealized_pl": (last - cost) * shares if cost else None,
            "unrealized_pl_pct": (last / cost - 1) * 100 if cost else None,
        }
        if cost and last <= cost * (1 - stop_pct):
            stop_breached = True
            reasons.insert(0, f"Price is {abs(last / cost - 1) * 100:.0f}% below your cost basis, past the stop-loss.")

    if held:
        if stop_breached or score <= -3:
            action = "SELL"
        elif score <= -2:
            action = "TRIM"
        elif score >= 2 and not near_withdrawal:
            action = "ADD"
        else:
            action = "HOLD"
    else:
        if score >= 2 and not near_withdrawal:
            action = "BUY"
        elif score <= -2:
            action = "AVOID"
        else:
            action = "WAIT"

    agreeing = sum(1 for v in votes if (v > 0) == (score > 0)) if score else 0
    agreement = agreeing / len(votes) if votes and score else 0.0
    if stop_breached or (abs(score) >= 3 and sma200 is not None):
        confidence = "High"
    elif abs(score) >= 2:
        confidence = "Medium"
    else:
        confidence = "Low"

    stop_ref = cost if (held and cost) else last
    levels = {
        "stop_loss_price": round(stop_ref * (1 - stop_pct), 2),
        "stop_loss_pct": stop_pct,
        "trend_break_price": round(sma200, 2) if sma200 is not None else None,
        "add_zone_low": round(sma50 * 0.97, 2),
        "add_zone_high": round(sma50 * 1.01, 2),
    }

    trade = suggest_trade(
        action,
        price=last,
        shares_held=float(holding["shares"]) if held else 0.0,
        portfolio_value=portfolio_value,
        cash=cash,
        max_position_pct=max_position_pct or MAX_POSITION_BY_RISK[band],
        max_pct_is_default=max_position_pct is None,
        band=band,
    )
    if action == "ADD" and trade.get("at_target"):
        action = "HOLD"
        reasons.append("Trend is positive, but the position is already at its maximum size for this client.")
        trade = suggest_trade("HOLD", price=last, shares_held=0, portfolio_value=None, cash=None, max_position_pct=1)

    return {
        **base,
        "action": action,
        "confidence": confidence,
        "score": score,
        "rule_agreement": round(agreement, 2),
        "indicators": {
            "price": round(last, 4),
            "sma50": round(sma50, 4),
            "sma200": round(sma200, 4) if sma200 is not None else None,
            "rsi14": round(rsi14, 2) if rsi14 is not None else None,
            "high_52w": round(high_52w, 4),
            "drawdown_from_high_pct": round(drawdown, 2),
            "return_3m_pct": round(ret_3m, 2) if ret_3m is not None else None,
        },
        "levels": levels,
        "position": position,
        "reasons": reasons,
        "client_fit": client_fit,
        "assumptions": assumptions,
        "risk_band": band,
        "years_to_first_withdrawal": horizon,
        "trade_suggestion": trade,
    }


def suggest_trade(
    action: str,
    *,
    price: float,
    shares_held: float,
    portfolio_value: float | None,
    cash: float | None,
    max_position_pct: float,
    max_pct_is_default: bool = True,
    band: str = "balanced",
) -> dict[str, Any]:
    """How many shares the signal implies buying or selling.

    Purchases are capped at ``max_position_pct`` of (holdings value + cash) and
    by available cash; trims sell about a third of the position.
    """
    none = {"side": None, "shares": 0, "est_value": 0.0, "rationale": "No trade suggested.", "assumptions": []}
    if price is None or price <= 0:
        return none

    if action == "SELL" and shares_held > 0:
        return {"side": "SELL", "shares": shares_held, "est_value": round(shares_held * price, 2),
                "rationale": f"Sell all {shares_held:g} shares.", "assumptions": []}

    if action == "TRIM" and shares_held > 0:
        qty = math.floor(shares_held / 3) if shares_held >= 3 else shares_held
        return {"side": "SELL", "shares": qty, "est_value": round(qty * price, 2),
                "rationale": f"Sell about a third of the position ({qty:g} of {shares_held:g} shares) to reduce risk.",
                "assumptions": []}

    if action in {"BUY", "ADD"}:
        assumptions = []
        if max_pct_is_default:
            assumptions.append(
                f"Max position size {max_position_pct * 100:.0f}% of the portfolio for a '{band}' client "
                "is an assumption; change it in Settings.")
        total = (portfolio_value or 0.0) + (cash or 0.0)
        if total <= 0:
            return {**none, "side": "BUY", "rationale": "Set your available cash in Settings to size this purchase.",
                    "assumptions": assumptions}
        room = max_position_pct * total - shares_held * price
        budget = room
        if cash is not None:
            budget = min(room, cash)
        else:
            assumptions.append("Available cash not set; size assumes cash is available.")
        qty = math.floor(budget / price) if budget > 0 else 0
        if qty < 1:
            if room < price:
                return {**none, "side": "BUY", "rationale": "Position is already at its target size.",
                        "assumptions": assumptions, "at_target": True}
            return {**none, "side": "BUY", "rationale": "Not enough available cash for one share.",
                    "assumptions": assumptions}
        return {"side": "BUY", "shares": qty, "est_value": round(qty * price, 2),
                "rationale": f"Buy {qty} shares to bring the position toward {max_position_pct * 100:.0f}% of the portfolio.",
                "assumptions": assumptions}
    return none

