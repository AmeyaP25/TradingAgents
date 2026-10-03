"""Persistence for the web workbench: login sessions, approved profile, holdings,
settings, price alerts, signal state and alert history.

Two backends share one interface:

* ``SupabaseStore`` calls ``wins_*`` Postgres functions over the Supabase REST
  API with the publishable key. The tables live in a private schema; every
  function verifies the password (bcrypt) or a session token itself.
* ``LocalStore`` keeps the same data in a JSON file, for offline use and tests.

Every data method takes the session token, so an expired or forged token is
rejected by the store itself rather than trusted from the caller.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

SESSION_TTL = timedelta(hours=12)
REMEMBER_TTL = timedelta(days=30)
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=5)

_TICKER_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")

# PBKDF2-SHA256 (salt, iterations, digest) for the single workbench account in
# local mode; Supabase mode verifies against a bcrypt hash in the database.
_LOCAL_ACCOUNT = {
    "username": "lions2-10468483",
    "salt": "b58b7662b5f3a67aa3da964391a1ee87",
    "iterations": 310000,
    "hash": "5b02ce19da15d6b7fe69c7c38e4137ba234d4430c55484217d183b854dcad21e",
}


class AuthError(Exception):
    """The session token is missing, expired or invalid."""


class StoreError(Exception):
    """The store could not complete the request."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_ticker(raw: str) -> str:
    ticker = (raw or "").strip().upper()
    if not _TICKER_RE.match(ticker):
        raise ValueError(f"Invalid ticker: {raw!r}")
    return ticker


class Holding(BaseModel):
    ticker: str
    shares: float = Field(ge=0)
    cost_basis: float | None = Field(default=None, ge=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)
    notes: str = ""
    added_at: str | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return normalize_ticker(value)


class PriceAlertIn(BaseModel):
    ticker: str
    direction: str = Field(pattern="^(above|below)$")
    price: float = Field(gt=0)
    note: str = ""

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return normalize_ticker(value)


class Store(Protocol):
    backend: str

    def login(self, username: str, password: str, remember: bool) -> dict[str, Any]: ...
    def session(self, token: str | None) -> dict[str, Any] | None: ...
    def logout(self, token: str | None) -> None: ...
    def get_profile(self, token: str) -> dict[str, Any] | None: ...
    def save_profile(self, token: str, profile: dict[str, Any]) -> None: ...
    def clear_profile(self, token: str) -> None: ...
    def list_holdings(self, token: str) -> list[dict[str, Any]]: ...
    def upsert_holding(self, token: str, holding: dict[str, Any]) -> list[dict[str, Any]]: ...
    def delete_holding(self, token: str, ticker: str) -> list[dict[str, Any]]: ...
    def get_settings(self, token: str) -> dict[str, Any]: ...
    def save_settings(self, token: str, settings: dict[str, Any]) -> dict[str, Any]: ...
    def list_price_alerts(self, token: str) -> list[dict[str, Any]]: ...
    def add_price_alert(self, token: str, alert: dict[str, Any]) -> list[dict[str, Any]]: ...
    def delete_price_alert(self, token: str, alert_id: int) -> list[dict[str, Any]]: ...
    def trigger_price_alert(self, token: str, alert_id: int) -> None: ...
    def get_signal_states(self, token: str) -> dict[str, Any]: ...
    def set_signal_state(self, token: str, ticker: str, action: str, price: float | None) -> None: ...
    def add_alert_event(self, token: str, event: dict[str, Any]) -> dict[str, Any]: ...
    def list_alert_events(self, token: str, limit: int = 100, unread_only: bool = False) -> list[dict[str, Any]]: ...
    def mark_alerts_read(self, token: str, ids: list[int] | None = None) -> None: ...


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

class SupabaseStore:
    backend = "supabase"

    def __init__(self, url: str, key: str, http=None, timeout: float = 10.0):
        import requests

        self.url = url.rstrip("/")
        self.key = key
        self.http = http or requests.Session()
        self.timeout = timeout

    def _rpc(self, name: str, **params: Any) -> Any:
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        try:
            res = self.http.post(
                f"{self.url}/rest/v1/rpc/{name}", headers=headers, json=params, timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001
            raise StoreError(f"Database unreachable: {type(exc).__name__}") from exc
        if res.status_code >= 400:
            try:
                body = res.json()
            except ValueError:
                body = {}
            if body.get("code") == "28000" or "invalid_session" in str(body.get("message", "")):
                raise AuthError("Session expired. Please sign in again.")
            raise StoreError(f"Database error ({res.status_code}): {body.get('message') or res.text[:200]}")
        if res.status_code == 204 or not res.content:
            return None
        return res.json()

    def login(self, username, password, remember):
        return self._rpc("wins_login", p_username=username, p_password=password, p_remember=bool(remember)) or {
            "ok": False, "error": "invalid_credentials"}

    def session(self, token):
        if not token:
            return None
        return self._rpc("wins_session", p_token=token)

    def logout(self, token):
        if token:
            self._rpc("wins_logout", p_token=token)

    def get_profile(self, token):
        return self._rpc("wins_get_profile", p_token=token)

    def save_profile(self, token, profile):
        self._rpc("wins_save_profile", p_token=token, p_profile=profile)

    def clear_profile(self, token):
        self._rpc("wins_clear_profile", p_token=token)

    def list_holdings(self, token):
        return self._rpc("wins_list_holdings", p_token=token) or []

    def upsert_holding(self, token, holding):
        return self._rpc("wins_upsert_holding", p_token=token, p_holding=holding) or []

    def delete_holding(self, token, ticker):
        return self._rpc("wins_delete_holding", p_token=token, p_ticker=ticker) or []

    def get_settings(self, token):
        return self._rpc("wins_get_settings", p_token=token) or {}

    def save_settings(self, token, settings):
        return self._rpc("wins_save_settings", p_token=token, p_settings=settings) or {}

    def list_price_alerts(self, token):
        return self._rpc("wins_list_price_alerts", p_token=token) or []

    def add_price_alert(self, token, alert):
        return self._rpc("wins_add_price_alert", p_token=token, p_alert=alert) or []

    def delete_price_alert(self, token, alert_id):
        return self._rpc("wins_delete_price_alert", p_token=token, p_id=int(alert_id)) or []

    def trigger_price_alert(self, token, alert_id):
        self._rpc("wins_trigger_price_alert", p_token=token, p_id=int(alert_id))

    def get_signal_states(self, token):
        return self._rpc("wins_get_signal_states", p_token=token) or {}

    def set_signal_state(self, token, ticker, action, price):
        self._rpc("wins_set_signal_state", p_token=token, p_ticker=ticker, p_action=action, p_price=price)

    def add_alert_event(self, token, event):
        return self._rpc("wins_add_alert_event", p_token=token, p_event=event) or {}

    def list_alert_events(self, token, limit=100, unread_only=False):
        return self._rpc("wins_list_alert_events", p_token=token, p_limit=int(limit), p_unread_only=bool(unread_only)) or []

    def mark_alerts_read(self, token, ids=None):
        self._rpc("wins_mark_alerts_read", p_token=token, p_ids=ids)


# ---------------------------------------------------------------------------
# Local JSON file
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_local_password(password: str) -> bool:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(_LOCAL_ACCOUNT["salt"]), _LOCAL_ACCOUNT["iterations"]
    ).hex()
    return hmac.compare_digest(digest, _LOCAL_ACCOUNT["hash"])


class LocalStore:
    backend = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "workbench.json"
        self._lock = threading.RLock()
        self._failures: dict[str, tuple[int, datetime | None]] = {}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def _user(self, data: dict[str, Any], token: str | None) -> str:
        sess = data.get("sessions", {}).get(_hash_token(token or ""))
        if not sess or datetime.fromisoformat(sess["expires_at"]) <= _now():
            raise AuthError("Session expired. Please sign in again.")
        return sess["username"]

    def _bucket(self, data: dict[str, Any], name: str, user: str, default: Any) -> Any:
        return data.setdefault(name, {}).setdefault(user, default)

    def login(self, username, password, remember):
        with self._lock:
            count, locked_until = self._failures.get(username, (0, None))
            if locked_until and locked_until > _now():
                return {"ok": False, "error": "account_locked", "locked_until": locked_until.isoformat()}
            if not (hmac.compare_digest(username or "", _LOCAL_ACCOUNT["username"]) and _check_local_password(password or "")):
                count += 1
                self._failures[username] = (count, _now() + LOCKOUT if count >= MAX_FAILED_LOGINS else None)
                return {"ok": False, "error": "invalid_credentials"}
            self._failures.pop(username, None)
            data = self._load()
            sessions = {
                k: v for k, v in data.get("sessions", {}).items()
                if datetime.fromisoformat(v["expires_at"]) > _now()
            }
            token = secrets.token_hex(32)
            expires = _now() + (REMEMBER_TTL if remember else SESSION_TTL)
            sessions[_hash_token(token)] = {
                "username": username, "expires_at": expires.isoformat(), "remember": bool(remember)}
            data["sessions"] = sessions
            self._save(data)
            return {"ok": True, "token": token, "username": username,
                    "expires_at": expires.isoformat(), "remember": bool(remember)}

    def session(self, token):
        with self._lock:
            data = self._load()
            try:
                user = self._user(data, token)
            except AuthError:
                return None
            sess = data["sessions"][_hash_token(token)]
            return {"username": user, "expires_at": sess["expires_at"], "remember": sess.get("remember", False)}

    def logout(self, token):
        with self._lock:
            data = self._load()
            data.get("sessions", {}).pop(_hash_token(token or ""), None)
            self._save(data)

    def get_profile(self, token):
        with self._lock:
            data = self._load()
            return data.get("profiles", {}).get(self._user(data, token))

    def save_profile(self, token, profile):
        with self._lock:
            data = self._load()
            data.setdefault("profiles", {})[self._user(data, token)] = profile
            self._save(data)

    def clear_profile(self, token):
        with self._lock:
            data = self._load()
            data.setdefault("profiles", {}).pop(self._user(data, token), None)
            self._save(data)

    def list_holdings(self, token):
        with self._lock:
            data = self._load()
            return list(data.get("holdings", {}).get(self._user(data, token), []))

    def upsert_holding(self, token, holding):
        with self._lock:
            data = self._load()
            user = self._user(data, token)
            items = [h for h in self._bucket(data, "holdings", user, []) if h["ticker"] != holding["ticker"]]
            existing = next((h for h in data["holdings"][user] if h["ticker"] == holding["ticker"]), None)
            items.append({**holding, "added_at": (existing or {}).get("added_at") or _now().isoformat()})
            data["holdings"][user] = items
            self._save(data)
            return items

    def delete_holding(self, token, ticker):
        with self._lock:
            data = self._load()
            user = self._user(data, token)
            items = [h for h in self._bucket(data, "holdings", user, []) if h["ticker"] != ticker]
            data["holdings"][user] = items
            self._bucket(data, "signal_state", user, {}).pop(ticker, None)
            self._save(data)
            return items

    def get_settings(self, token):
        with self._lock:
            data = self._load()
            return dict(data.get("settings", {}).get(self._user(data, token), {}))

    def save_settings(self, token, settings):
        with self._lock:
            data = self._load()
            data.setdefault("settings", {})[self._user(data, token)] = settings
            self._save(data)
            return settings

    def list_price_alerts(self, token):
        with self._lock:
            data = self._load()
            items = data.get("price_alerts", {}).get(self._user(data, token), [])
            return sorted(items, key=lambda a: a["created_at"], reverse=True)

    def add_price_alert(self, token, alert):
        with self._lock:
            data = self._load()
            user = self._user(data, token)
            data["next_id"] = data.get("next_id", 0) + 1
            self._bucket(data, "price_alerts", user, []).append({
                **alert, "id": data["next_id"], "active": True,
                "created_at": _now().isoformat(), "triggered_at": None})
            self._save(data)
        return self.list_price_alerts(token)

    def delete_price_alert(self, token, alert_id):
        with self._lock:
            data = self._load()
            user = self._user(data, token)
            data.setdefault("price_alerts", {})[user] = [
                a for a in data["price_alerts"].get(user, []) if a["id"] != int(alert_id)]
            self._save(data)
        return self.list_price_alerts(token)

    def trigger_price_alert(self, token, alert_id):
        with self._lock:
            data = self._load()
            for a in self._bucket(data, "price_alerts", self._user(data, token), []):
                if a["id"] == int(alert_id):
                    a["active"] = False
                    a["triggered_at"] = _now().isoformat()
            self._save(data)

    def get_signal_states(self, token):
        with self._lock:
            data = self._load()
            return dict(data.get("signal_state", {}).get(self._user(data, token), {}))

    def set_signal_state(self, token, ticker, action, price):
        with self._lock:
            data = self._load()
            self._bucket(data, "signal_state", self._user(data, token), {})[ticker] = {
                "last_action": action, "last_price": price, "updated_at": _now().isoformat()}
            self._save(data)

    def add_alert_event(self, token, event):
        with self._lock:
            data = self._load()
            user = self._user(data, token)
            data["next_id"] = data.get("next_id", 0) + 1
            row = {**event, "id": data["next_id"], "is_read": False, "created_at": _now().isoformat()}
            self._bucket(data, "alert_events", user, []).append(row)
            data["alert_events"][user] = data["alert_events"][user][-500:]
            self._save(data)
            return row

    def list_alert_events(self, token, limit=100, unread_only=False):
        with self._lock:
            data = self._load()
            items = data.get("alert_events", {}).get(self._user(data, token), [])
            if unread_only:
                items = [e for e in items if not e.get("is_read")]
            return sorted(items, key=lambda e: e["created_at"], reverse=True)[: max(1, min(limit, 500))]

    def mark_alerts_read(self, token, ids=None):
        with self._lock:
            data = self._load()
            wanted = set(ids or [])
            for e in self._bucket(data, "alert_events", self._user(data, token), []):
                if ids is None or e["id"] in wanted:
                    e["is_read"] = True
            self._save(data)


def make_store(results_dir: str | Path) -> Store:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = (os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or "").strip()
    if url and key:
        return SupabaseStore(url, key)
    return LocalStore(Path(results_dir) / "workbench")
