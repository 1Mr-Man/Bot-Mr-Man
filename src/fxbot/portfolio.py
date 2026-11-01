from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
import math
from pathlib import Path
from typing import Any, Protocol


class _AccountInfo(Protocol):
    login: int
    trade_mode: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    currency: str


class _TerminalApi(Protocol):
    ACCOUNT_TRADE_MODE_DEMO: int
    ACCOUNT_TRADE_MODE_CONTEST: int

    def initialize(self, *, path: str, portable: bool, timeout: int) -> bool: ...
    def shutdown(self) -> None: ...
    def account_info(self) -> _AccountInfo | None: ...
    def positions_get(self) -> tuple[Any, ...] | None: ...
    def history_deals_get(self, date_from: datetime, date_to: datetime) -> tuple[Any, ...] | None: ...
    def last_error(self) -> Any: ...


@dataclass(frozen=True)
class AccountSnapshot:
    profile_id: str
    profile_name: str
    expected_login: int
    verified: bool
    login: int | None
    account_mode: str
    balance: float | None
    equity: float | None
    margin: float | None
    free_margin: float | None
    margin_level: float | None
    currency: str | None
    open_position_count: int | None
    closed_deals_today: int | None
    realized_pnl_today: float | None
    checked_at_utc: str
    error: str | None


def snapshots_from_profiles(profiles: list[Any], *, api: _TerminalApi) -> list[dict[str, Any]]:
    return [asdict(snapshot_profile(profile, api=api)) for profile in profiles]


def snapshot_profile(profile: Any, *, api: _TerminalApi) -> AccountSnapshot:
    checked_at = datetime.now(timezone.utc)
    base = {
        "profile_id": profile.id,
        "profile_name": profile.name,
        "expected_login": profile.account_id,
        "checked_at_utc": checked_at.isoformat(),
    }
    if not profile.terminal_path:
        return AccountSnapshot(
            **base,
            verified=False,
            login=None,
            account_mode="unknown",
            balance=None,
            equity=None,
            margin=None,
            free_margin=None,
            margin_level=None,
            currency=None,
            open_position_count=None,
            closed_deals_today=None,
            realized_pnl_today=None,
            error="Configure a separate MT5 terminal path for this profile",
        )

    initialized = False
    try:
        terminal_path = Path(profile.terminal_path)
        portable_directory = (
            terminal_path if terminal_path.is_dir() else terminal_path.parent
        )
        if not portable_directory.is_dir():
            return _unavailable(
                base,
                api,
                "Configured terminal or its data directory does not exist",
            )
        if terminal_path.is_file() and terminal_path.name.casefold() not in {
            "terminal.exe",
            "terminal64.exe",
        }:
            return _unavailable(base, api, "Configured executable must be terminal.exe or terminal64.exe")
        initialized = bool(
            api.initialize(
                path=str(terminal_path),
                portable=True,
                timeout=60_000,
            )
        )
        if not initialized:
            return _unavailable(base, api, f"Could not initialize profile terminal")
        account = api.account_info()
        if account is None:
            return _unavailable(base, api, "MT5 returned no account details")
        mode_name = _account_mode(api, account.trade_mode)
        if account.trade_mode not in {
            api.ACCOUNT_TRADE_MODE_DEMO,
            api.ACCOUNT_TRADE_MODE_CONTEST,
        }:
            return _unavailable(
                base,
                api,
                "Profile terminal is not logged in to a demo or contest account",
                login=account.login,
                mode=mode_name,
            )
        if account.login != profile.account_id:
            return _unavailable(
                base,
                api,
                "Terminal login does not match this profile; metrics withheld",
                login=account.login,
                mode=mode_name,
            )
        numeric_fields = (
            account.balance,
            account.equity,
            account.margin,
            account.margin_free,
        )
        if not all(math.isfinite(value) for value in numeric_fields):
            return _unavailable(
                base,
                api,
                "Account returned non-finite balance or margin values",
                login=account.login,
                mode=mode_name,
            )
        margin_level = float(account.margin_level)
        if not math.isfinite(margin_level):
            margin_level = 0.0 if account.margin == 0 else None
        positions = api.positions_get()
        if positions is None:
            return _unavailable(
                base,
                api,
                "Could not load account positions",
                login=account.login,
                mode=mode_name,
            )
        start = datetime.combine(checked_at.date(), time.min, tzinfo=timezone.utc)
        deals = api.history_deals_get(start, checked_at)
        if deals is None:
            return _unavailable(
                base,
                api,
                "Could not load today's account history",
                login=account.login,
                mode=mode_name,
            )
        closed = [deal for deal in deals if _is_closed(api, deal)]
        pnl = sum(_deal_total(deal) for deal in closed)
        return AccountSnapshot(
            **base,
            verified=True,
            login=account.login,
            account_mode=mode_name,
            balance=float(account.balance),
            equity=float(account.equity),
            margin=float(account.margin),
            free_margin=float(account.margin_free),
            margin_level=margin_level,
            currency=str(account.currency),
            open_position_count=len(positions),
            closed_deals_today=len(closed),
            realized_pnl_today=pnl,
            error=None,
        )
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
        return _unavailable(base, api, f"Account check failed: {type(exc).__name__}")
    finally:
        if initialized:
            api.shutdown()


def _unavailable(
    base: dict[str, Any],
    api: _TerminalApi,
    error: str,
    *,
    login: int | None = None,
    mode: str = "unknown",
) -> AccountSnapshot:
    return AccountSnapshot(
        **base,
        verified=False,
        login=login,
        account_mode=mode,
        balance=None,
        equity=None,
        margin=None,
        free_margin=None,
        margin_level=None,
        currency=None,
        open_position_count=None,
        closed_deals_today=None,
        realized_pnl_today=None,
        error=error,
    )


def _account_mode(api: _TerminalApi, mode: int) -> str:
    if mode == api.ACCOUNT_TRADE_MODE_DEMO:
        return "demo"
    if mode == api.ACCOUNT_TRADE_MODE_CONTEST:
        return "contest"
    return "live"


def _is_closed(api: _TerminalApi, deal: Any) -> bool:
    return getattr(deal, "entry", None) in {
        getattr(api, "DEAL_ENTRY_OUT", 1),
        getattr(api, "DEAL_ENTRY_OUT_BY", 3),
        getattr(api, "DEAL_ENTRY_INOUT", 2),
    }


def _deal_total(deal: Any) -> float:
    values = (
        float(deal.profit),
        float(deal.commission),
        float(deal.swap),
        float(deal.fee),
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("MT5 history includes non-finite deal amounts")
    return sum(values)
