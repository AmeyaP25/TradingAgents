"""Local HTTP API and dashboard for TradingAgents runs."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tradingagents.alerts import AlertMonitor, MarketCache, build_signal
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.client_profile import ClientProfile, extract_client_profile
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.portfolio import PortfolioContext
from tradingagents.workbench_store import (
    REMEMBER_TTL,
    AuthError,
    Holding,
    PriceAlertIn,
    Store,
    StoreError,
    make_store,
    normalize_ticker,
)

SESSION_COOKIE = "ta_session"
PUBLIC_PATHS = {"/login", "/auth/login", "/auth/logout", "/health"}


class ExtractProfileRequest(BaseModel):
    case_study_text: str = Field(min_length=1)


class AnalyzeRequest(BaseModel):
    ticker: str
    trade_date: str
    asset_type: str = "stock"
    selected_analysts: list[str] = Field(default_factory=lambda: ["market", "social", "news", "fundamentals"])
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    portfolio: dict[str, Any] | None = None
    client_profile: dict[str, Any] | None = None
    case_study_text: str | None = None


class AnalyzeResponse(BaseModel):
    signal: str
    final_rating: str
    final_trade_decision: str
    structured_recommendation_report: dict[str, Any]
    client_constraint_violations: list[dict[str, Any]]
    client_profile_data: dict[str, Any]
    final_state: dict[str, Any]


class BacktestRequest(BaseModel):
    tickers: list[str]
    start_date: str
    end_date: str
    every_n_days: int = 7
    asset_type: str = "stock"
    selected_analysts: list[str] = Field(default_factory=lambda: ["market", "social", "news", "fundamentals"])
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    portfolio: dict[str, Any] | None = None
    client_profile: dict[str, Any] | None = None
    case_study_text: str | None = None
    run_id: str | None = None


class BacktestResponse(BaseModel):
    run_id: str
    log_path: str
    cells_run: int
    skipped: int
    failures: list[tuple[str, str, str]]
    settlement_failures: list[tuple[str, str]]
    summary: str


def _webui_dir() -> Path:
    return Path(__file__).resolve().parent / "webui"


class ProfileSaveRequest(BaseModel):
    client_profile: dict[str, Any]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)
    remember: bool = False


class SettingsIn(BaseModel):
    cash_available: float | None = Field(default=None, ge=0)
    max_position_pct: float | None = Field(default=None, gt=0, le=1)
    alerts_enabled: bool = True
    browser_notifications: bool = True
    sound: bool = True


class MarkReadRequest(BaseModel):
    ids: list[int] | None = None


class SessionCache:
    """Remembers recently verified tokens so each request is not a database round trip."""

    def __init__(self, store: Store, ttl: float = 60.0):
        self.store = store
        self.ttl = ttl
        self._seen: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        key = self._key(token)
        with self._lock:
            hit = self._seen.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        info = self.store.session(token)
        with self._lock:
            if info:
                self._seen[key] = (time.monotonic(), info)
            else:
                self._seen.pop(key, None)
        return info

    def forget(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._seen.pop(self._key(token), None)


def create_app(
    store: Store | None = None,
    market: MarketCache | None = None,
    start_monitor: bool | None = None,
) -> FastAPI:
    store = store or make_store(DEFAULT_CONFIG["results_dir"])
    market = market or MarketCache()
    monitor = AlertMonitor(store, market, interval=float(os.getenv("WORKBENCH_ALERT_SECONDS", "60")))
    sessions = SessionCache(store)
    if start_monitor is None:
        start_monitor = os.getenv("WORKBENCH_MONITOR", "1") != "0"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_monitor:
            monitor.start()
        yield
        monitor.stop()

    app = FastAPI(title="TradingAgents Local API", version="0.4.0", lifespan=lifespan)
    app.state.store = store
    app.state.monitor = monitor
    webui_dir = _webui_dir()
    app.mount("/assets", StaticFiles(directory=str(webui_dir)), name="assets")

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/assets/"):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE)
        try:
            info = sessions.get(token)
        except StoreError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        if not info:
            if request.method == "GET" and not path.startswith("/api/") and "text/html" in request.headers.get("accept", ""):
                response: Response = RedirectResponse("/login", status_code=303)
            else:
                response = JSONResponse({"detail": "Not signed in."}, status_code=401)
            if token:
                response.delete_cookie(SESSION_COOKIE)
            return response
        request.state.token = token
        request.state.user = info.get("username")
        monitor.set_token(token)
        return await call_next(request)

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError):
        sessions.forget(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"detail": str(exc)}, status_code=401)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.exception_handler(StoreError)
    async def _store_error(_request: Request, exc: StoreError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    def token(request: Request) -> str:
        return request.state.token

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(webui_dir / "index.html")

    @app.get("/login", response_model=None)
    def login_page(request: Request):
        if sessions.get(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
        return FileResponse(webui_dir / "login.html")

    @app.post("/auth/login")
    def login(req: LoginRequest, request: Request) -> JSONResponse:
        result = store.login(req.username.strip(), req.password, req.remember)
        if not result.get("ok"):
            if result.get("error") == "account_locked":
                return JSONResponse({"detail": "Too many failed attempts. Try again in a few minutes."}, status_code=429)
            return JSONResponse({"detail": "Incorrect username or password."}, status_code=401)
        response = JSONResponse({"ok": True, "username": result["username"], "remember": result["remember"]})
        response.set_cookie(
            SESSION_COOKIE,
            result["token"],
            max_age=int(REMEMBER_TTL.total_seconds()) if req.remember else None,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        monitor.set_token(result["token"])
        return response

    @app.post("/auth/logout")
    def logout(request: Request) -> JSONResponse:
        tok = request.cookies.get(SESSION_COOKIE)
        sessions.forget(tok)
        try:
            store.logout(tok)
        except (AuthError, StoreError):
            pass
        monitor.set_token(None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/auth/me")
    def me(request: Request) -> dict[str, Any]:
        info = sessions.get(request.cookies.get(SESSION_COOKIE)) or {}
        return {"username": info.get("username"), "expires_at": info.get("expires_at"),
                "remember": info.get("remember"), "backend": store.backend}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/meta")
    def meta() -> dict[str, Any]:
        return {
            "defaults": {
                "asset_type": "stock",
                "selected_analysts": ["market", "social", "news", "fundamentals"],
            },
            "asset_types": ["stock", "crypto"],
            "analysts": ["market", "social", "news", "fundamentals"],
        }

    @app.post("/client-profile/extract")
    def extract_profile(req: ExtractProfileRequest) -> dict[str, Any]:
        return extract_profile_payload(req.case_study_text)

    @app.post("/analyze", response_model=AnalyzeResponse)
    def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
        return analyze_payload(req)

    @app.post("/backtest", response_model=BacktestResponse)
    def backtest(req: BacktestRequest) -> BacktestResponse:
        return backtest_payload(req)

    @app.get("/api/profile")
    def get_profile(tok: str = Depends(token)) -> dict[str, Any]:
        profile = store.get_profile(tok)
        return {"approved": profile is not None, "client_profile": profile}

    @app.post("/api/profile")
    def save_profile(req: ProfileSaveRequest, tok: str = Depends(token)) -> dict[str, Any]:
        profile = _resolve_profile(req.client_profile, None).model_dump()
        store.save_profile(tok, profile)
        return {"approved": True, "client_profile": profile}

    @app.delete("/api/profile")
    def delete_profile(tok: str = Depends(token)) -> dict[str, Any]:
        store.clear_profile(tok)
        return {"approved": False, "client_profile": None}

    @app.get("/api/holdings")
    def list_holdings(tok: str = Depends(token)) -> list[dict[str, Any]]:
        return store.list_holdings(tok)

    @app.post("/api/holdings")
    def upsert_holding(holding: Holding, tok: str = Depends(token)) -> list[dict[str, Any]]:
        return store.upsert_holding(tok, holding.model_dump(exclude={"added_at"}))

    @app.delete("/api/holdings/{ticker}")
    def delete_holding(ticker: str, tok: str = Depends(token)) -> list[dict[str, Any]]:
        return store.delete_holding(tok, _ticker_or_400(ticker))

    @app.get("/api/settings")
    def get_settings(tok: str = Depends(token)) -> dict[str, Any]:
        return SettingsIn.model_validate(store.get_settings(tok) or {}).model_dump()

    @app.post("/api/settings")
    def save_settings(req: SettingsIn, tok: str = Depends(token)) -> dict[str, Any]:
        return store.save_settings(tok, req.model_dump())

    @app.get("/api/quotes")
    def quotes(tickers: str) -> list[dict[str, Any]]:
        symbols = [_ticker_or_400(t) for t in tickers.split(",") if t.strip()]
        if not symbols:
            raise HTTPException(status_code=400, detail="Provide at least one ticker.")
        if len(symbols) > 25:
            raise HTTPException(status_code=400, detail="At most 25 tickers per request.")
        return [market.quote(s) for s in symbols]

    @app.get("/api/history/{ticker}")
    def history(ticker: str, range: str = "6mo") -> dict[str, Any]:  # noqa: A002
        try:
            return market.history(_ticker_or_400(ticker), range)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/signal/{ticker}")
    def signal(ticker: str, tok: str = Depends(token)) -> dict[str, Any]:
        return signal_payload(store, tok, _ticker_or_400(ticker), market)

    @app.get("/api/alerts")
    def list_alerts(limit: int = 100, unread_only: bool = False, tok: str = Depends(token)) -> dict[str, Any]:
        events = store.list_alert_events(tok, limit, unread_only)
        unread = sum(1 for e in events if not e.get("is_read")) if not unread_only else len(events)
        return {
            "events": events,
            "unread": unread,
            "monitor": {
                "running": start_monitor,
                "interval_seconds": monitor.interval,
                "last_run": monitor.last_run,
                "last_error": monitor.last_error,
            },
        }

    @app.post("/api/alerts/read")
    def mark_read(req: MarkReadRequest, tok: str = Depends(token)) -> dict[str, Any]:
        store.mark_alerts_read(tok, req.ids)
        return {"ok": True}

    @app.post("/api/alerts/check")
    def check_now(tok: str = Depends(token)) -> dict[str, Any]:
        created = monitor.run_once(tok)
        return {"created": created}

    @app.get("/api/price-alerts")
    def list_price_alerts(tok: str = Depends(token)) -> list[dict[str, Any]]:
        return store.list_price_alerts(tok)

    @app.post("/api/price-alerts")
    def add_price_alert(req: PriceAlertIn, tok: str = Depends(token)) -> list[dict[str, Any]]:
        return store.add_price_alert(tok, req.model_dump())

    @app.delete("/api/price-alerts/{alert_id}")
    def delete_price_alert(alert_id: int, tok: str = Depends(token)) -> list[dict[str, Any]]:
        return store.delete_price_alert(tok, alert_id)

    return app


def _ticker_or_400(raw: str) -> str:
    try:
        return normalize_ticker(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def signal_payload(store: Store, token: str, ticker: str, market: MarketCache) -> dict[str, Any]:
    return build_signal(
        ticker,
        market=market,
        holdings=store.list_holdings(token),
        profile_data=store.get_profile(token),
        settings=store.get_settings(token) or {},
    )


def extract_profile_payload(case_study_text: str) -> dict[str, Any]:
    profile = extract_client_profile(case_study_text)
    return profile.model_dump()


def analyze_payload(req: AnalyzeRequest) -> AnalyzeResponse:
    config = DEFAULT_CONFIG.copy()
    config.update(req.config_overrides or {})
    portfolio_context = _resolve_portfolio(req.portfolio)
    profile = _resolve_profile(req.client_profile, req.case_study_text)

    try:
        graph = TradingAgentsGraph(
            selected_analysts=tuple(req.selected_analysts),
            config=config,
            debug=False,
        )
        final_state, signal = graph.propagate(
            req.ticker,
            req.trade_date,
            asset_type=req.asset_type,
            portfolio=portfolio_context,
            client_profile=profile,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc

    return AnalyzeResponse(
        signal=signal,
        final_rating=final_state.get("final_rating", ""),
        final_trade_decision=final_state.get("final_trade_decision", ""),
        structured_recommendation_report=final_state.get("structured_recommendation_report", {}),
        client_constraint_violations=final_state.get("client_constraint_violations", []),
        client_profile_data=final_state.get("client_profile_data", {}),
        final_state=final_state,
    )


def _resolve_profile(profile_data: dict[str, Any] | None, case_study_text: str | None) -> ClientProfile | None:
    if profile_data is not None and case_study_text:
        raise HTTPException(status_code=400, detail="Provide either client_profile or case_study_text, not both.")
    if profile_data is not None:
        try:
            return ClientProfile.model_validate(profile_data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid client_profile payload: {exc}") from exc
    if case_study_text:
        return extract_client_profile(case_study_text)
    return None


def _resolve_portfolio(portfolio_data: dict[str, Any] | None) -> PortfolioContext | None:
    if portfolio_data is None:
        return None
    try:
        return PortfolioContext.model_validate(portfolio_data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid portfolio payload: {exc}") from exc


def backtest_payload(req: BacktestRequest) -> BacktestResponse:
    if not req.tickers:
        raise HTTPException(status_code=400, detail="At least one ticker is required.")
    config = DEFAULT_CONFIG.copy()
    config.update(req.config_overrides or {})
    portfolio_context = _resolve_portfolio(req.portfolio)
    profile = _resolve_profile(req.client_profile, req.case_study_text)

    try:
        dates = iter_grid(req.start_date, req.end_date, req.every_n_days)
        result = run_backtest(
            tickers=req.tickers,
            dates=dates,
            config=config,
            asset_type=req.asset_type,
            portfolio=portfolio_context,
            client_profile=profile,
            selected_analysts=tuple(req.selected_analysts),
            run_id=req.run_id,
        )
        summary = summarize(result).render()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Backtest failed: {exc}") from exc

    return BacktestResponse(
        run_id=result.run_id,
        log_path=str(result.log_path),
        cells_run=result.cells_run,
        skipped=result.skipped,
        failures=result.failures,
        settlement_failures=result.settlement_failures,
        summary=summary,
    )
