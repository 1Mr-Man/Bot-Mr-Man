from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Iterable, Protocol, cast

from fxbot.models import Bar


class _Rate(Protocol):
    def __getitem__(self, key: str) -> int | float:
        ...


class _AccountInfo(Protocol):
    trade_mode: int


class _Mt5Api(Protocol):
    TIMEFRAME_H4: int
    TIMEFRAME_H1: int
    TIMEFRAME_M5: int
    TIMEFRAME_M15: int
    TIMEFRAME_M30: int
    ACCOUNT_TRADE_MODE_DEMO: int
    ACCOUNT_TRADE_MODE_CONTEST: int

    def initialize(self, *, timeout: int) -> bool:
        ...

    def shutdown(self) -> None:
        ...

    def account_info(self) -> _AccountInfo | None:
        ...

    def copy_rates_range(
        self,
        symbol: str,
        timeframe: int,
        date_from: datetime,
        date_to: datetime,
    ) -> Iterable[_Rate] | None:
        ...

    def last_error(self) -> object:
        ...


class Mt5BarSource:
    """Read completed MT5 bars from an already configured demo account."""

    TIMEFRAME_MINUTES = {
        "M5": 5,
        "M15": 15,
        "M30": 30,
        "H1": 60,
        "H4": 240,
    }

    def __init__(
        self,
        symbol: str = "EURUSD",
        timeout_ms: int = 60_000,
        timeframe: str = "H1",
    ) -> None:
        if not symbol.strip():
            raise ValueError("symbol must not be empty")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        if timeframe not in self.TIMEFRAME_MINUTES:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        self.symbol = symbol
        self.timeout_ms = timeout_ms
        self.timeframe = timeframe

    @property
    def _timeframe_minutes(self) -> int:
        return self.TIMEFRAME_MINUTES[self.timeframe]

    def load(self, start: datetime, end: datetime) -> list[Bar]:
        start_utc = _as_utc(start, "start")
        end_utc = _as_utc(end, "end")
        if start_utc >= end_utc:
            raise ValueError("start must be earlier than end")
        if end_utc > datetime.now(timezone.utc):
            raise ValueError("end must not be in the future")

        api = self._load_api()
        if not api.initialize(timeout=self.timeout_ms):
            raise RuntimeError(f"Could not initialize MetaTrader 5: {api.last_error()}")

        try:
            account = api.account_info()
            if account is None:
                raise RuntimeError(f"Could not read MetaTrader 5 account: {api.last_error()}")
            allowed_modes = {
                api.ACCOUNT_TRADE_MODE_DEMO,
                api.ACCOUNT_TRADE_MODE_CONTEST,
            }
            if account.trade_mode not in allowed_modes:
                raise RuntimeError("MT5 data loading requires a demo or contest account")

            rates = api.copy_rates_range(
                self.symbol,
                self._timeframe_value(api),
                start_utc,
                end_utc,
            )
            if rates is None:
                raise RuntimeError(f"Could not load MT5 rates: {api.last_error()}")

            completed: list[Bar] = []
            now = datetime.now(timezone.utc)
            bar_duration = timedelta(minutes=self._timeframe_minutes)
            for rate in rates:
                timestamp = datetime.fromtimestamp(int(rate["time"]), timezone.utc)
                if timestamp < start_utc or timestamp > end_utc:
                    continue
                if timestamp + bar_duration > now:
                    continue
                completed.append(
                    Bar(
                        symbol=self.symbol,
                        timestamp=timestamp,
                        open=float(rate["open"]),
                        high=float(rate["high"]),
                        low=float(rate["low"]),
                        close=float(rate["close"]),
                        volume=float(rate["tick_volume"]),
                    )
                )
            if not completed:
                raise RuntimeError(
                    f"MT5 returned no completed {self.timeframe} bars for "
                    f"{self.symbol} in the requested range"
                )
            if any(
                current.timestamp <= previous.timestamp
                for previous, current in zip(completed, completed[1:])
            ):
                raise RuntimeError(
                    f"MT5 returned duplicate or out-of-order {self.timeframe} bars"
                )
            return completed
        finally:
            api.shutdown()

    @staticmethod
    def _load_api() -> _Mt5Api:
        try:
            module = import_module("MetaTrader5")
        except ImportError as exc:
            raise RuntimeError(
                'MetaTrader5 is optional. Install it with: python -m pip install -e ".[mt5]"'
            ) from exc
        return cast(_Mt5Api, module)

    def _timeframe_value(self, api: _Mt5Api) -> int:
        return {
            "M5": api.TIMEFRAME_M5,
            "M15": api.TIMEFRAME_M15,
            "M30": api.TIMEFRAME_M30,
            "H1": api.TIMEFRAME_H1,
            "H4": api.TIMEFRAME_H4,
        }[self.timeframe]


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)
