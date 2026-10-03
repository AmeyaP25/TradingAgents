"""Raw live quote and close-history reads for the workbench UI."""

from __future__ import annotations

import yfinance as yf

from tradingagents.dataflows.vendors.yahoo.common import yf_retry


def fetch_fast_quote(symbol: str) -> dict | None:
    """Fast-info fields for ``symbol``; None when Yahoo answers it has nothing."""

    def fetch() -> dict:
        info = yf.Ticker(symbol).fast_info
        return {
            "price": info.get("lastPrice"),
            "previous_close": info.get("previousClose"),
            "open": info.get("open"),
            "day_high": info.get("dayHigh"),
            "day_low": info.get("dayLow"),
            "year_high": info.get("yearHigh"),
            "year_low": info.get("yearLow"),
            "market_cap": info.get("marketCap"),
            "currency": info.get("currency") or "USD",
        }

    return yf_retry(fetch, max_retries=1, base_delay=1.0)


def fetch_close_history(symbol: str, period: str, interval: str) -> list[tuple]:
    """(timestamp, close) pairs, oldest first; empty when Yahoo has no prices."""

    def fetch():
        return yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)

    frame = yf_retry(fetch, max_retries=1, base_delay=1.0)
    if frame is None or frame.empty or "Close" not in frame:
        return []
    return list(frame["Close"].items())
