from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import uuid4

from fxbot.consensus import STRATEGY_IDS, make_strategies, scan_latest_signals
from fxbot.data.mt5 import Mt5BarSource
from fxbot.models import Side
from fxbot.paper import PaperConfig, run_paper_backtest
from fxbot.risk import RiskLimits
from fxbot.stages import build_stage_targets, stage_reached_by_simulation
from fxbot.portfolio import snapshots_from_profiles


MAX_PROFILES = 5
MAX_SYMBOLS = 50
_ALLOWED_TIMEFRAMES = {"M5", "M15", "M30", "H1"}
_ALLOWED_STRATEGY_MODES = {"consensus", "single"}


@dataclass(frozen=True)
class DemoProfile:
    id: str
    name: str
    account_id: int
    symbols: tuple[str, ...]
    max_spreads: dict[str, float]
    timeframe: str = "M5"
    risk_per_trade: float = 0.001
    max_daily_loss: float = 0.02
    max_volume: float = 0.01
    max_total_open_risk: float = 0.01
    max_open_positions: int = 1
    strategy_mode: str = "consensus"
    selected_strategy: str = "sma"
    enabled_strategies: tuple[str, ...] = STRATEGY_IDS
    consensus_votes: int = 3
    terminal_path: str = ""
    initial_balance: float = 10_000.0
    target_stages: int = 30
    stage_growth_rate: float = 0.3
    backtest_start: str = "2025-01-01"
    backtest_end: str = "2025-01-31"
    test_stop_distance: float = 0.001
    test_spread: float = 0.0001

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DemoProfile":
        if not isinstance(payload, dict):
            raise ValueError("Profile payload must be a JSON object")
        profile_id = payload.get("id") or uuid4().hex
        if not isinstance(profile_id, str) or len(profile_id) > 64:
            raise ValueError("Profile id is invalid")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
            raise ValueError("Profile name must be 1-64 characters")
        raw_account_id = payload.get("account_id")
        if isinstance(raw_account_id, bool) or not str(raw_account_id).isdigit():
            raise ValueError("Demo login must be a positive integer")
        try:
            account_id = int(raw_account_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Demo login must be a positive integer") from exc
        if account_id <= 0:
            raise ValueError("Demo login must be a positive integer")

        raw_symbols = payload.get("symbols")
        if isinstance(raw_symbols, str):
            symbols = tuple(item.strip() for item in raw_symbols.split(","))
        elif isinstance(raw_symbols, list) and all(isinstance(item, str) for item in raw_symbols):
            symbols = tuple(item.strip() for item in raw_symbols)
        else:
            raise ValueError("Symbols must be a comma-separated string or list")
        if not symbols or any(not item for item in symbols):
            raise ValueError("At least one non-empty symbol is required")
        if len(symbols) > MAX_SYMBOLS:
            raise ValueError(f"No more than {MAX_SYMBOLS} symbols can be configured")
        if len({item.upper() for item in symbols}) != len(symbols):
            raise ValueError("Duplicate symbols are not allowed")
        if any(len(item) > 32 or any(c in item for c in "\r\n\0") for item in symbols):
            raise ValueError("A symbol name is invalid")

        raw_spreads = payload.get("max_spreads")
        if isinstance(raw_spreads, str):
            spreads: dict[str, float] = {}
            try:
                for entry in raw_spreads.split(","):
                    symbol, value = entry.split("=", maxsplit=1)
                    key = symbol.strip().upper()
                    limit = float(value.strip())
                    if not key or not math.isfinite(limit) or limit <= 0:
                        raise ValueError
                    if key in spreads:
                        raise ValueError
                    spreads[key] = limit
            except ValueError as exc:
                raise ValueError("Spread limits must be SYMBOL=positive-number entries") from exc
        elif isinstance(raw_spreads, dict):
            spreads = {}
            for symbol, value in raw_spreads.items():
                if not isinstance(symbol, str):
                    raise ValueError("Spread limit symbol names must be strings")
                try:
                    limit = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Spread limits must be positive numbers") from exc
                key = symbol.strip().upper()
                if not key or not math.isfinite(limit) or limit <= 0 or key in spreads:
                    raise ValueError("Spread limits must be unique positive numbers")
                spreads[key] = limit
        else:
            raise ValueError("Per-symbol spread limits are required")
        if set(spreads) != {item.upper() for item in symbols}:
            raise ValueError("Provide exactly one broker-specific spread limit per symbol")

        timeframe = payload.get("timeframe", "M5")
        if timeframe not in _ALLOWED_TIMEFRAMES:
            raise ValueError("Timeframe must be M5, M15, M30, or H1")
        limits = {
            "risk_per_trade": (0 < _number(payload, "risk_per_trade", 0.001) <= 0.0025),
            "max_daily_loss": (0 < _number(payload, "max_daily_loss", 0.02) <= 0.05),
            "max_volume": (0 < _number(payload, "max_volume", 0.01) <= 100),
            "max_total_open_risk": (0 < _number(payload, "max_total_open_risk", 0.01) <= 0.01),
        }
        if not all(limits.values()):
            raise ValueError("Risk, daily-loss, volume, and aggregate-risk settings exceed allowed bounds")
        max_open = payload.get("max_open_positions", 1)
        if isinstance(max_open, bool) or not isinstance(max_open, int):
            raise ValueError("Maximum open positions must be an integer")
        if not 1 <= max_open <= min(10, len(symbols)):
            raise ValueError("Maximum open positions must be 1-10 and no more than the symbol count")
        strategy_mode = payload.get("strategy_mode", "consensus")
        if strategy_mode not in _ALLOWED_STRATEGY_MODES:
            raise ValueError("Strategy mode must be consensus or single")
        strategy_mode = str(strategy_mode)
        selected_strategy = payload.get("selected_strategy", "sma")
        if selected_strategy not in STRATEGY_IDS:
            raise ValueError("Selected strategy is unknown")
        raw_enabled = payload.get("enabled_strategies", list(STRATEGY_IDS))
        if not isinstance(raw_enabled, list) or not all(
            isinstance(item, str) and item in STRATEGY_IDS for item in raw_enabled
        ):
            raise ValueError("Enabled strategies must be a list of known strategy IDs")
        enabled_strategies = tuple(raw_enabled)
        if (
            not enabled_strategies
            or len(set(enabled_strategies)) != len(enabled_strategies)
        ):
            raise ValueError("Select one or more unique strategies")
        if strategy_mode == "single" and selected_strategy not in enabled_strategies:
            raise ValueError("The selected strategy must be enabled")
        if strategy_mode == "consensus" and not any(
            item != "rsi" for item in enabled_strategies
        ):
            raise ValueError("Consensus mode requires at least one trend strategy for confirmation")
        consensus_votes = payload.get("consensus_votes", 3)
        if (
            isinstance(consensus_votes, bool)
            or not isinstance(consensus_votes, int)
            or not 1 <= consensus_votes <= len(enabled_strategies)
        ):
            raise ValueError("Consensus votes must be 1 through the number of enabled strategies")
        terminal_path = payload.get("terminal_path", "")
        if not isinstance(terminal_path, str) or len(terminal_path) > 1024 or any(
            character in terminal_path for character in "\r\n\0"
        ):
            raise ValueError("MT5 terminal path is invalid")
        if terminal_path.strip() and not Path(terminal_path).is_absolute():
            raise ValueError("MT5 terminal path must be an absolute executable or data folder path")
        initial_balance = _number(payload, "initial_balance", 10_000.0)
        target_stages = payload.get("target_stages", 30)
        stage_growth_rate = _number(payload, "stage_growth_rate", 0.3)
        if not 0 < initial_balance <= 1_000_000_000:
            raise ValueError("Initial simulation balance must be positive and at most 1 billion")
        if isinstance(target_stages, bool) or not isinstance(target_stages, int) or not 1 <= target_stages <= 100:
            raise ValueError("Target stages must be an integer from 1 to 100")
        if not 0 < stage_growth_rate <= 1:
            raise ValueError("Stage growth rate must be greater than 0 and at most 1")
        backtest_start = payload.get("backtest_start", "2025-01-01")
        backtest_end = payload.get("backtest_end", "2025-01-31")
        if not isinstance(backtest_start, str) or not isinstance(backtest_end, str):
            raise ValueError("Backtest date range must use YYYY-MM-DD strings")
        try:
            parsed_start = datetime.strptime(backtest_start, "%Y-%m-%d")
            parsed_end = datetime.strptime(backtest_end, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Backtest date range must use YYYY-MM-DD") from exc
        if parsed_end < parsed_start or (parsed_end - parsed_start).days > 3650:
            raise ValueError("Backtest date range must be ordered and at most 10 years")
        test_stop_distance = _number(payload, "test_stop_distance", 0.001)
        test_spread = _number(payload, "test_spread", 0.0001)
        if test_stop_distance <= 0 or test_spread < 0:
            raise ValueError("Backtest stop distance must be positive and spread nonnegative")
        return cls(
            id=profile_id,
            name=name.strip(),
            account_id=account_id,
            symbols=symbols,
            max_spreads=spreads,
            timeframe=timeframe,
            risk_per_trade=_number(payload, "risk_per_trade", 0.001),
            max_daily_loss=_number(payload, "max_daily_loss", 0.02),
            max_volume=_number(payload, "max_volume", 0.01),
            max_total_open_risk=_number(payload, "max_total_open_risk", 0.01),
            max_open_positions=max_open,
            strategy_mode=strategy_mode,
            selected_strategy=selected_strategy,
            enabled_strategies=enabled_strategies,
            consensus_votes=consensus_votes,
            terminal_path=terminal_path.strip(),
            initial_balance=initial_balance,
            target_stages=target_stages,
            stage_growth_rate=stage_growth_rate,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
            test_stop_distance=test_stop_distance,
            test_spread=test_spread,
        )


class ProfileStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def list(self) -> list[DemoProfile]:
        with self._lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                raw_profiles = data["profiles"]
                if not isinstance(raw_profiles, list):
                    raise ValueError
                profiles = [DemoProfile.from_payload(item) for item in raw_profiles]
            except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("Local profile file is invalid; inspect it before editing") from exc
            if len(profiles) > MAX_PROFILES or len({p.id for p in profiles}) != len(profiles):
                raise RuntimeError("Local profile file exceeds limits or has duplicate ids")
            if len({p.account_id for p in profiles}) != len(profiles):
                raise RuntimeError("Local profiles contain duplicate MT5 demo logins")
            if len({p.account_id for p in profiles}) != len(profiles):
                raise RuntimeError("Local profiles contain duplicate MT5 demo logins")
            paths = [str(Path(p.terminal_path).resolve()).casefold() for p in profiles if p.terminal_path]
            if len(set(paths)) != len(paths):
                raise RuntimeError("Each monitored profile must use a separate MT5 terminal path")
            return profiles

    def save(self, payload: dict[str, Any]) -> DemoProfile:
        profile = DemoProfile.from_payload(payload)
        if len(profile.id) > 64 or any(character in profile.id for character in "\r\n\0"):
            raise ValueError("Profile id is invalid")
        with self._lock:
            profiles = self.list()
            match_index = next((i for i, item in enumerate(profiles) if item.id == profile.id), None)
            if match_index is None:
                if len(profiles) >= MAX_PROFILES:
                    raise ValueError(f"Maximum of {MAX_PROFILES} demo account profiles reached")
                profiles.append(profile)
            else:
                profiles[match_index] = profile
            if len({p.account_id for p in profiles}) != len(profiles):
                raise ValueError("Each profile must use a different MT5 demo login")
            configured_paths = [str(Path(p.terminal_path).resolve()).casefold() for p in profiles if p.terminal_path]
            if len(set(configured_paths)) != len(configured_paths):
                raise ValueError("Each terminal-configured account needs a distinct MT5 terminal path")
            self._write(profiles)
        return profile

    def get(self, profile_id: str) -> DemoProfile:
        for profile in self.list():
            if profile.id == profile_id:
                return profile
        raise ValueError("Demo account profile not found")

    def _write(self, profiles: list[DemoProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": 1, "profiles": [asdict(profile) for profile in profiles]}
        temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, self.path)


def _number(payload: dict[str, Any], key: str, default: float) -> float:
    try:
        value = float(payload.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    return value


class BotController:
    """Starts at most one guarded demo worker and requests stop via its kill switch."""

    def __init__(
        self,
        project_root: Path,
        state_dir: Path,
        store: ProfileStore,
        *,
        api_factory: Any | None = None,
    ) -> None:
        self.project_root = project_root
        self.state_dir = state_dir
        self.store = store
        self._api_factory = api_factory
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._profile_id: str | None = None
        self._kill_switch: Path | None = None
        self._log_path: Path | None = None
        self._started_at: float | None = None
        self._last_exit_code: int | None = None

    def start(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            self._refresh_process()
            if self._process is not None:
                raise ValueError("A demo worker is already running; stop it before starting another")
            profile = self.store.get(profile_id)
            run_id = uuid4().hex
            run_dir = self.state_dir / "workers"
            log_dir = self.state_dir / "logs"
            run_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)
            kill_switch = run_dir / f"{run_id}.stop"
            log_path = log_dir / f"{run_id}.log"
            script = self.project_root / "examples" / "mt5_demo_bot.py"
            if not script.is_file():
                raise RuntimeError("Continuous demo runner was not found in project folder")
            command = [
                sys.executable,
                str(script),
                "--symbols", ",".join(profile.symbols),
                "--timeframe", profile.timeframe,
                "--expected-account-id", str(profile.account_id),
                "--max-spreads", ",".join(
                    f"{symbol}={profile.max_spreads[symbol.upper()]}"
                    for symbol in profile.symbols
                ),
                "--risk-per-trade", str(profile.risk_per_trade),
                "--max-daily-loss", str(profile.max_daily_loss),
                "--max-volume", str(profile.max_volume),
                "--max-open-positions", str(profile.max_open_positions),
                "--max-total-open-risk", str(profile.max_total_open_risk),
                "--strategy-mode", profile.strategy_mode,
                "--strategies", ",".join(profile.enabled_strategies),
                "--consensus-votes", str(profile.consensus_votes),
                "--run-demo-bot",
                "--confirm", "RUN DEMO BOT",
                "--kill-switch", str(kill_switch),
            ]
            environment = os.environ.copy()
            environment.pop("FINNHUB_API_KEY", None)
            with log_path.open("ab") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    shell=False,
                )
            self._process = process
            self._profile_id = profile.id
            self._kill_switch = kill_switch
            self._log_path = log_path
            self._started_at = time.time()
            self._last_exit_code = None
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_process()
            if self._process is None:
                return self.status()
            assert self._kill_switch is not None
            self._kill_switch.parent.mkdir(parents=True, exist_ok=True)
            self._kill_switch.write_text("stop\n", encoding="utf-8")
            return self.status()

    def account_snapshots(self) -> list[dict[str, Any]]:
        if self._api_factory is None:
            from importlib import import_module

            try:
                api = import_module("MetaTrader5")
            except ImportError:
                return [
                    {
                        "profile_id": profile.id,
                        "profile_name": profile.name,
                        "expected_login": profile.account_id,
                        "verified": False,
                        "error": "MetaTrader5 package is not installed",
                    }
                    for profile in self.store.list()
                ]
        else:
            api = self._api_factory()
        return snapshots_from_profiles(self.store.list(), api=api)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_process()
            log_text = ""
            if self._log_path and self._log_path.exists():
                try:
                    log_text = self._log_path.read_text(encoding="utf-8", errors="replace")[-40000:]
                except OSError:
                    log_text = "Unable to read worker log."
            profile = None
            if self._profile_id:
                try:
                    profile = self.store.get(self._profile_id).name
                except (ValueError, RuntimeError):
                    profile = None
            return {
                "running": self._process is not None,
                "stop_requested": bool(self._kill_switch and self._kill_switch.exists()),
                "profile_id": self._profile_id,
                "profile_name": profile,
                "pid": self._process.pid if self._process is not None else None,
                "last_exit_code": self._last_exit_code,
                "started_at": self._started_at,
                "log": log_text,
            }

    def _refresh_process(self) -> None:
        if self._process is None:
            return
        code = self._process.poll()
        if code is not None:
            self._last_exit_code = code
            self._process = None


class DashboardServer:
    def __init__(
        self,
        *,
        project_root: Path,
        state_dir: Path,
        api_factory: Any | None = None,
        port: int = 8765,
        token: str | None = None,
    ) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        if not 1024 <= port <= 65535:
            raise ValueError("Port must be between 1024 and 65535")
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        if not self.token or any(character in self.token for character in "\r\n\0"):
            raise ValueError("Dashboard token is invalid")
        self.store = ProfileStore(state_dir / "profiles.json")
        self.controller = BotController(project_root, state_dir, self.store, api_factory=api_factory)
        self._origin = f"http://127.0.0.1:{port}"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "FXBotLocal/1.0"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, payload: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(payload)

            def _json(self, status: int, data: Any) -> None:
                self._send(status, json.dumps(data, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

            def _local_host(self) -> bool:
                return self.headers.get("Host", "").lower() in {
                    f"127.0.0.1:{outer.port}", f"localhost:{outer.port}"
                }

            def _authorized(self) -> bool:
                return self._local_host() and secrets.compare_digest(
                    self.headers.get("Authorization", ""), f"Bearer {outer.token}"
                )

            def _body(self) -> dict[str, Any]:
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("Content-Type must be application/json")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ValueError("Invalid Content-Length") from exc
                if not 0 < length <= 32768:
                    raise ValueError("Request body must be 1-32768 bytes")
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("Request body must contain valid JSON") from exc
                if not isinstance(data, dict):
                    raise ValueError("JSON body must be an object")
                return data

            def do_GET(self) -> None:
                if not self._local_host():
                    self._json(403, {"error": "Local host access only"})
                    return
                if self.path == "/":
                    self._send(200, _dashboard_html().encode("utf-8"), "text/html; charset=utf-8")
                    return
                if not self._authorized():
                    self._json(401, {"error": "Dashboard token required"})
                    return
                if self.path == "/api/status":
                    try:
                        profiles = [asdict(p) for p in outer.store.list()]
                    except RuntimeError as exc:
                        self._json(500, {"error": str(exc)})
                        return
                    self._json(
                        200,
                        {
                            "profiles": profiles,
                            "accounts": outer.controller.account_snapshots(),
                            "worker": outer.controller.status(),
                        },
                    )
                    return
                if self.path.startswith("/api/stages/"):
                    profile_id = self.path.removeprefix("/api/stages/")
                    try:
                        profile = outer.store.get(profile_id)
                    except (ValueError, RuntimeError) as exc:
                        self._json(404, {"error": str(exc)})
                        return
                    targets = build_stage_targets(
                        profile.initial_balance,
                        stages=profile.target_stages,
                        growth_rate=profile.stage_growth_rate,
                    )
                    self._json(
                        200,
                        {
                            "balance_formula": "initial_balance * (1 + growth_rate) ** stage",
                            "initial_balance": profile.initial_balance,
                            "growth_rate": profile.stage_growth_rate,
                            "targets": [asdict(target) for target in targets],
                            "caveat": "Targets are arithmetic milestones, not forecasts or guarantees.",
                        },
                    )
                    return
                self._json(404, {"error": "Not found"})

            def do_POST(self) -> None:
                if not self._local_host():
                    self._json(403, {"error": "Local host access only"})
                    return
                if not self._authorized():
                    self._json(401, {"error": "Dashboard token required"})
                    return
                if self.headers.get("Origin") != outer._origin:
                    self._json(403, {"error": "Same-origin requests only"})
                    return
                try:
                    data = self._body()
                    if self.path == "/api/profiles":
                        profile = outer.store.save(data)
                        self._json(200, {"profile": asdict(profile)})
                    elif self.path == "/api/start":
                        profile_id = data.get("profile_id")
                        if not isinstance(profile_id, str):
                            raise ValueError("profile_id is required")
                        self._json(200, {"worker": outer.controller.start(profile_id)})
                    elif self.path in {"/api/backtest", "/api/simulate"}:
                        result = outer._run_profile_backtest(data)
                        self._json(200, {"simulation": result})
                    elif self.path == "/api/stop":
                        self._json(200, {"worker": outer.controller.stop()})
                    else:
                        self._json(404, {"error": "Not found"})
                except (ValueError, RuntimeError, OSError) as exc:
                    self._json(400, {"error": str(exc)})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True

    def _run_profile_backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile_id = payload.get("profile_id")
        symbol = payload.get("symbol")
        timeframe = payload.get("timeframe")
        start_text = payload.get("start")
        end_text = payload.get("end")
        if not isinstance(profile_id, str) or not isinstance(symbol, str):
            raise ValueError("profile_id and symbol are required")
        if not isinstance(timeframe, str) or timeframe not in Mt5BarSource.TIMEFRAME_MINUTES:
            raise ValueError("A supported timeframe is required")
        if symbol not in self.store.get(profile_id).symbols:
            raise ValueError("Backtest symbol must belong to the selected profile")
        try:
            start_date = datetime.strptime(start_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_date = datetime.strptime(end_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("Backtest dates must use YYYY-MM-DD") from exc
        if end_date < start_date or end_date - start_date > timedelta(days=3650):
            raise ValueError("Backtest date range must be ordered and at most 10 years")
        profile = self.store.get(profile_id)
        if isinstance(payload.get("initial_balance"), (list, dict, bool)):
            raise ValueError("initial_balance must be a number")
        strategy = payload.get("strategy", profile.selected_strategy)
        if profile.strategy_mode == "single":
            strategy = profile.selected_strategy
        if strategy not in STRATEGY_IDS:
            raise ValueError("Unknown configured strategy")
        initial_balance = _number(payload, "initial_balance", profile.initial_balance)
        stages = profile.target_stages
        growth_rate = profile.stage_growth_rate
        stop_distance = _number(payload, "stop_distance", _default_stop(symbol))
        spread = _number(payload, "spread", profile.max_spreads[symbol.upper()])
        if stop_distance <= 0 or spread < 0:
            raise ValueError("Stop distance must be positive and spread nonnegative")
        if not math.isclose(spread, profile.max_spreads[symbol.upper()], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("Backtest spread must match the selected profile's broker spread assumption")
        bars = Mt5BarSource(symbol=symbol, timeframe=timeframe).load(
            start_date,
            end_date + timedelta(days=1),
        )
        strategy_name, strategy_impl = make_strategies(
            lookback=20,
            strategy_ids=(strategy,),
        )[0]
        config = PaperConfig(
            initial_equity=initial_balance,
            stop_distance=stop_distance,
            spread=spread,
            risk_limits=RiskLimits(
                risk_per_trade=profile.risk_per_trade,
                max_daily_loss=profile.max_daily_loss,
            ),
        )
        result = run_paper_backtest(bars, strategy_impl, config)
        win_count = sum(trade.net_pnl > 0 for trade in result.trades)
        targets = build_stage_targets(
            initial_balance,
            stages=stages,
            growth_rate=growth_rate,
        )
        reached = stage_reached_by_simulation(
            initial_balance,
            result.final_equity,
            stages=stages,
            growth_rate=growth_rate,
        )
        return {
            "profile_name": profile.name,
            "expected_demo_login": profile.account_id,
            "strategy": strategy_name,
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": len(bars),
            "initial_balance": initial_balance,
            "final_balance": result.final_balance,
            "final_equity": result.final_equity,
            "closed_trades": len(result.trades),
            "trades_to_stage_30": (
                len(result.trades)
                if stages >= 30 and reached >= 30
                else None
            ),
            "wins": win_count,
            "losses": sum(trade.net_pnl < 0 for trade in result.trades),
            "win_rate_pct": (win_count / len(result.trades) * 100) if result.trades else None,
            "max_drawdown_pct": _max_drawdown_pct(result),
            "reached_stage": stage_reached_by_simulation(
                initial_balance,
                result.final_equity,
                stages=stages,
                growth_rate=growth_rate,
            ),
            "stage_target_formula": "initial_balance * (1 + growth_rate) ** stage",
            "stage_targets": [asdict(item) for item in targets],
            "caveat": "Historical simulation does not establish future reliability or predict trades-to-target.",
        }

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.25)

    def shutdown(self) -> None:
        self.controller.stop()
        self.httpd.shutdown()
        self.httpd.server_close()


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FX Bot Dashboard</title><style>
:root{color-scheme:dark;font:16px system-ui;background:#10151c;color:#e9eef5}body{max-width:1000px;margin:2rem auto;padding:0 1rem}h1{margin-bottom:.25rem}.warning{background:#482b18;padding:1rem;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}section{background:#1b2530;padding:1rem;border-radius:10px;margin:1rem 0}label{display:block;margin:.7rem 0 .25rem}input,select,textarea,button{font:inherit;padding:.6rem;border-radius:6px;border:1px solid #526273;background:#101820;color:#f5f7fa;width:100%;box-sizing:border-box}button{cursor:pointer;background:#245a7a;margin-top:.6rem}button.stop{background:#8a302b}button:disabled{opacity:.5}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:35rem;overflow:auto;background:#080c10;padding:.8rem;border-radius:6px}.muted{color:#afbdca}.row{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}.danger{color:#ffb7aa;font-weight:700}
</style></head><body><h1>FX Bot local dashboard</h1><p class="muted">Local-only control • MT5 demo accounts only • one worker at a time</p>
<p class="warning"><b>Demo execution can submit orders.</b> This dashboard is local-only. Never expose its port publicly. The bot rejects live accounts; signals and strategy results do not guarantee profit.</p>
<div class="grid"><section><h2>Demo account profile</h2><form id="profile-form"><input type="hidden" id="profile-id"><label>Profile name</label><input id="name" required maxlength="64" placeholder="Main demo"><label>MT5 demo login number</label><input id="account-id" required inputmode="numeric" placeholder="12345678"><label>Symbols, comma-separated</label><input id="symbols" required value="XAUUSD"><label>Per-symbol spread caps (price units)</label><input id="spreads" required value="XAUUSD=0.50"><label>Trading timeframe</label><select id="timeframe"><option>M5</option><option>M15</option><option>M30</option><option>H1</option></select><div class="row"><div><label>Strategy mode</label><select id="strategy-mode"><option value="consensus">Combined consensus</option><option value="single">One selected rule</option></select></div><div><label>Selected rule (single mode)</label><select id="selected-strategy"><option value="sma">SMA 10/30 trend (default)</option><option value="donchian">Donchian breakout</option><option value="ema">EMA 12/26 trend</option><option value="macd">MACD 12/26/9 trend</option><option value="rsi">RSI 14 mean reversion</option></select></div></div><label>Rules enabled for this account (comma-separated IDs)</label><input id="enabled-strategies" value="sma,donchian,ema,macd,rsi"><label>Consensus vote threshold</label><input id="consensus-votes" type="number" min="1" max="5" value="3"><label>MT5 terminal executable path (separate terminal/data directory per account required)</label><input id="terminal-path" maxlength="1024" placeholder="C:\\Program Files\\MetaTrader 5\\terminal64.exe"><label>Historical backtest period (UTC dates)</label><div class="row"><input id="backtest-start" type="date" value="2025-01-01"><input id="backtest-end" type="date" value="2025-01-31"></div><label>Backtest stop distance and spread (broker price units)</label><div class="row"><input id="test-stop-distance" type="number" step="any" value="0.001"><input id="test-spread" type="number" step="any" value="0.0001"></div><label>Simulated starting balance</label><input id="initial-balance" type="number" min="1" step="any" value="10000"><label>Target stages (initial × (1 + growth rate)^stage)</label><div class="row"><input id="target-stages" type="number" min="1" max="100" value="30"><input id="growth-rate" type="number" min="0.001" max="1" step="0.01" value="0.30"></div><div class="row"><div><label>Risk per trade (fraction)</label><input id="risk" type="number" step="0.0001" value="0.001"></div><div><label>Max daily loss (fraction)</label><input id="daily" type="number" step="0.001" value="0.02"></div></div><div class="row"><div><label>Max lots</label><input id="volume" type="number" step="0.01" value="0.01"></div><div><label>Max open positions</label><input id="positions" type="number" min="1" max="10" value="1"></div></div><label>Aggregate open stop risk (fraction; max 0.01)</label><input id="total-risk" type="number" step="0.001" value="0.01"><button type="submit">Save demo profile</button></form><p id="profile-msg" class="muted"></p></section>
<section><h2>Worker controls</h2><label>Select saved profile</label><select id="profile-select"></select><button id="start">Start guarded demo bot</button><button id="stop" class="stop">Request bot stop</button><div id="status" class="muted">Loading…</div><p class="danger">Start submits orders automatically when its configured entry rules and safety checks pass. Profiles save individual strategy settings, but only one worker/account is supported. The MT5 terminal path is not yet used for routing. Stop uses the bot kill-switch; existing broker-side stops remain active.</p></section></div>
<section><h2>Account monitoring</h2><p class="muted">Each profile uses its own MT5 terminal path. Metrics are shown only when the terminal proves the expected demo login.</p><div style="overflow:auto"><table id="accounts"><thead><tr><th>Profile</th><th>Login/status</th><th>Balance</th><th>Equity</th><th>Margin</th><th>Free margin</th><th>Margin level</th><th>Open positions</th><th>Closed trades today</th><th>Realized PnL today</th></tr></thead><tbody></tbody></table></div></section>
<section><h2>Historical strategy simulation</h2><p class="warning">Uses historical MT5 bars and simplified fill assumptions; it cannot tell you whether a strategy is reliable in the future. It never submits orders.</p><label>Profile</label><select id="backtest-profile"></select><label>Symbol</label><select id="backtest-symbol"></select><button id="run-backtest">Run historical simulation</button><pre id="backtest-result">No simulation run.</pre></section>
<section><h2>Worker log</h2><pre id="log">No log yet.</pre></section><script>
(()=>{const apiToken=sessionStorage.getItem('fxbot.local.token')||new URLSearchParams(location.hash.slice(1)).get('token');if(!apiToken){document.body.innerHTML='<h1>Dashboard token missing</h1><p>Restart the dashboard command and open its printed local URL.</p>';return;}sessionStorage.setItem('fxbot.local.token',apiToken);history.replaceState(null,'',location.pathname);const $=id=>document.getElementById(id);let profiles=[];async function request(path,method='GET',body){const response=await fetch(path,{method,headers:{'Authorization':'Bearer '+apiToken,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(data.error||('HTTP '+response.status));return data;}function setProfile(p){$('profile-id').value=p.id||'';$('name').value=p.name||'';$('account-id').value=p.account_id||'';$('symbols').value=p.symbols.join(',');$('spreads').value=Object.entries(p.max_spreads).map(([k,v])=>k+'='+v).join(',');$('timeframe').value=p.timeframe;$('strategy-mode').value=p.strategy_mode||'consensus';$('selected-strategy').value=p.selected_strategy||'sma';$('enabled-strategies').value=(p.enabled_strategies||['sma','donchian','ema','macd','rsi']).join(',');$('consensus-votes').value=p.consensus_votes||3;$('terminal-path').value=p.terminal_path||'';$('backtest-start').value=p.backtest_start||'2025-01-01';$('backtest-end').value=p.backtest_end||'2025-01-31';$('test-stop-distance').value=p.test_stop_distance||0.001;$('test-spread').value=p.test_spread??0.0001;$('initial-balance').value=p.initial_balance||10000;$('target-stages').value=p.target_stages||30;$('growth-rate').value=p.stage_growth_rate??0.3;$('risk').value=p.risk_per_trade;$('daily').value=p.max_daily_loss;$('volume').value=p.max_volume;$('positions').value=p.max_open_positions;$('total-risk').value=p.max_total_open_risk;}function render(data){profiles=data.profiles;$('profile-select').replaceChildren(...profiles.map(p=>{const o=document.createElement('option');o.value=p.id;o.textContent=p.name+' — demo '+p.account_id;return o;}));$('backtest-profile').replaceChildren(...profiles.map(p=>{const o=document.createElement('option');o.value=p.id;o.textContent=p.name+' — demo '+p.account_id;return o;}));const selected=profiles.find(p=>p.id===$('backtest-profile').value)||profiles[0];$('backtest-symbol').replaceChildren(...(selected?selected.symbols:[]).map(symbol=>{const o=document.createElement('option');o.value=symbol;o.textContent=symbol;return o;}));const tbody=$('accounts').querySelector('tbody');tbody.replaceChildren(...data.accounts.map(a=>{const row=document.createElement('tr');const values=[a.profile_name,(a.verified?'Verified demo ':'UNVERIFIED ')+(a.login||a.expected_login),a.balance??'—',a.equity??'—',a.margin??'—',a.free_margin??'—',a.margin_level??'—',a.open_position_count??'—',a.closed_deals_today??'—',a.realized_pnl_today??'—'];for(const value of values){const cell=document.createElement('td');cell.textContent=String(value);row.appendChild(cell);}if(a.error){const cell=document.createElement('td');cell.textContent=a.error;row.appendChild(cell);}return row;}));if(profiles.length&&!$('profile-id').value)setProfile(profiles[0]);const w=data.worker;$('status').textContent=w.running?'RUNNING — '+w.profile_name+' (PID '+w.pid+')'+(w.stop_requested?' — stop requested':''):('Stopped'+(w.last_exit_code!==null?' (exit '+w.last_exit_code+')':''));$('log').textContent=w.log||'No worker log yet.';$('log').scrollTop=$('log').scrollHeight;$('start').disabled=w.running||!profiles.length;$('stop').disabled=!w.running;}async function refresh(){try{render(await request('/api/status'));}catch(e){$('status').textContent='Dashboard error: '+e.message;}}$('profile-select').onchange=()=>{const p=profiles.find(x=>x.id===$('profile-select').value);if(p)setProfile(p);};$('backtest-profile').onchange=()=>{const p=profiles.find(x=>x.id===$('backtest-profile').value);$('backtest-symbol').replaceChildren(...(p?p.symbols:[]).map(symbol=>{const o=document.createElement('option');o.value=symbol;o.textContent=symbol;return o;}));};$('profile-form').onsubmit=async e=>{e.preventDefault();const payload={id:$('profile-id').value||undefined,name:$('name').value,account_id:$('account-id').value,symbols:$('symbols').value,max_spreads:$('spreads').value,timeframe:$('timeframe').value,strategy_mode:$('strategy-mode').value,selected_strategy:$('selected-strategy').value,enabled_strategies:$('enabled-strategies').value.split(',').map(x=>x.trim()).filter(Boolean),consensus_votes:Number($('consensus-votes').value),terminal_path:$('terminal-path').value,backtest_start:$('backtest-start').value,backtest_end:$('backtest-end').value,test_stop_distance:$('test-stop-distance').value,test_spread:$('test-spread').value,initial_balance:$('initial-balance').value,target_stages:Number($('target-stages').value),stage_growth_rate:$('growth-rate').value,risk_per_trade:$('risk').value,max_daily_loss:$('daily').value,max_volume:$('volume').value,max_open_positions:Number($('positions').value),max_total_open_risk:$('total-risk').value,initial_balance:$('initial-balance').value,target_stages:Number($('target-stages').value),stage_growth_rate:$('growth-rate').value,backtest_start:$('backtest-start').value,backtest_end:$('backtest-end').value,test_stop_distance:$('test-stop-distance').value,test_spread:$('test-spread').value};try{const d=await request('/api/profiles','POST',payload);$('profile-id').value=d.profile.id;$('profile-msg').textContent='Profile saved locally. No password is stored.';await refresh();$('profile-select').value=d.profile.id;}catch(err){$('profile-msg').textContent=err.message;}};$('start').onclick=async()=>{if(!confirm('Start the continuous demo bot for the selected profile? It may submit orders automatically on a verified DEMO account.'))return;try{await request('/api/start','POST',{profile_id:$('profile-select').value});await refresh();}catch(e){alert(e.message);}};$('stop').onclick=async()=>{if(!confirm('Request the bot stop? Existing broker-side stop-losses remain active.'))return;try{await request('/api/stop','POST',{});await refresh();}catch(e){alert(e.message);}};$('run-backtest').onclick=async()=>{const profile=profiles.find(p=>p.id===$('backtest-profile').value);if(!profile)return;$('backtest-result').textContent='Running historical simulation…';try{const d=await request('/api/backtest','POST',{profile_id:profile.id,symbol:$('backtest-symbol').value,timeframe:profile.timeframe,start:profile.backtest_start,end:profile.backtest_end,initial_balance:profile.initial_balance,stop_distance:profile.test_stop_distance,spread:profile.test_spread});$('backtest-result').textContent=JSON.stringify(d.simulation,null,2);}catch(e){$('backtest-result').textContent='Simulation failed: '+e.message;}};refresh();setInterval(refresh,2000);})();
</script></body></html>"""


def _default_stop(symbol: str) -> float:
    normalized = symbol.upper()
    if normalized.startswith(("XAU", "GOLD")):
        return 5.0
    if normalized.startswith("XAG"):
        return 0.5
    if normalized.startswith(("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD")):
        return 0.001
    return 0.01


def _max_drawdown_pct(result: Any) -> float:
    values = [result.initial_equity, *(point.equity for point in result.equity_curve)]
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    return max_drawdown * 100


def default_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "FXBot"
    return Path.home() / ".fxbot"
