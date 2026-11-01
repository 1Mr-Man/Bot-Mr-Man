from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from fxbot.execution.mt5_demo import (
    DemoOrderSettings,
    close_demo_position,
    prepare_demo_order,
    submit_demo_order,
)
from fxbot.models import Bar, Side
from examples.mt5_demo_bot import _is_retryable_data_error, run_bar


class FakeTradingApi:
    ACCOUNT_TRADE_MODE_DEMO = 0
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_INOUT = 2
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self, *, trade_mode: int = 0, login: int = 123456, positions: tuple = ()) -> None:
        self.trade_mode = trade_mode
        self.login = login
        self.positions = positions
        self.sent: list[dict[str, object]] = []
        self.checks: list[dict[str, object]] = []
        self.quote = SimpleNamespace(bid=1.1000, ask=1.1001)
        self.trading_allowed = True

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            trade_mode=self.trade_mode,
            login=self.login,
            balance=10_000.0,
            equity=10_000.0,
            trade_allowed=self.trading_allowed,
        )

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(trade_allowed=True)

    def symbol_info(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            visible=True,
            trade_mode=4,
            point=0.00001,
            digits=5,
            trade_stops_level=10,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            filling_mode=self.SYMBOL_FILLING_FOK,
        )

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    def symbol_info_tick(self, symbol: str) -> SimpleNamespace:
        return self.quote

    def positions_get(self) -> tuple:
        return self.positions

    def history_deals_get(self, date_from: datetime, date_to: datetime) -> tuple:
        self.history_request = (date_from, date_to)
        return ()

    def order_calc_profit(
        self,
        order_type: int,
        symbol: str,
        volume: float,
        price_open: float,
        price_close: float,
    ) -> float:
        return -100.0 * volume

    def order_check(self, request: dict[str, object]) -> SimpleNamespace:
        self.checks.append(request)
        return SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, request: dict[str, object]) -> SimpleNamespace:
        self.sent.append(request)
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            comment="Done",
            order=42,
            deal=43,
            volume=request["volume"],
            price=request["price"],
        )

    def last_error(self) -> tuple[int, str]:
        return (0, "no error")


class DemoOrderExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeTradingApi()
        self.settings = DemoOrderSettings(
            expected_account_id=123456,
            risk_per_trade=0.001,
            max_daily_loss=0.02,
            max_volume=0.01,
            max_spread=0.0003,
        )

    def test_only_transient_mt5_data_errors_are_retryable(self) -> None:
        self.assertTrue(
            _is_retryable_data_error(
                RuntimeError(
                    "Could not initialize MetaTrader 5: (-6, 'Authorization failed')"
                )
            )
        )
        self.assertTrue(
            _is_retryable_data_error(RuntimeError("Could not load MT5 rates: timeout"))
        )
        self.assertFalse(
            _is_retryable_data_error(
                RuntimeError("MT5 data loading requires a demo or contest account")
            )
        )

    def test_prepares_bounded_market_order_with_protective_stop(self) -> None:
        preview = prepare_demo_order(
            self.api,
            symbol="EURUSD",
            side=Side.LONG,
            stop_distance=0.001,
            settings=self.settings,
        )

        self.assertEqual(preview.volume, 0.01)
        self.assertEqual(preview.request["sl"], 1.0991)
        self.assertEqual(preview.estimated_risk, 1.0)
        self.assertEqual(preview.request["tp"], 1.1021)
        self.assertEqual(self.api.sent, [])

    def test_daily_pnl_uses_only_closed_deals_not_open_entries(self) -> None:
        class MixedHistoryApi(FakeTradingApi):
            def history_deals_get(self, date_from: datetime, date_to: datetime) -> tuple:
                return (
                    SimpleNamespace(
                        profit=0.0,
                        commission=-5.0,
                        swap=0.0,
                        fee=0.0,
                        entry=0,
                    ),
                    SimpleNamespace(
                        profit=10.0,
                        commission=-1.0,
                        swap=0.0,
                        fee=0.0,
                        entry=self.DEAL_ENTRY_OUT,
                    ),
                )

        api = MixedHistoryApi()
        preview = prepare_demo_order(
            api,
            symbol="EURUSD",
            side=Side.LONG,
            stop_distance=0.001,
            settings=self.settings,
        )

        self.assertEqual(preview.daily_realized_pnl, 9.0)

    def test_refuses_daily_pnl_calculation_for_deal_without_entry_type(self) -> None:
        class IncompleteHistoryApi(FakeTradingApi):
            def history_deals_get(self, date_from: datetime, date_to: datetime) -> tuple:
                return (SimpleNamespace(profit=0.0, commission=0.0, swap=0.0, fee=0.0),)

        with self.assertRaisesRegex(RuntimeError, "without an entry type"):
            prepare_demo_order(
                IncompleteHistoryApi(),
                symbol="EURUSD",
                side=Side.LONG,
                stop_distance=0.001,
                settings=self.settings,
            )

    def test_short_take_profit_is_two_times_stop_distance(self) -> None:
        preview = prepare_demo_order(
            self.api,
            symbol="EURUSD",
            side=Side.SHORT,
            stop_distance=0.001,
            settings=self.settings,
        )

        self.assertEqual(preview.request["sl"], 1.101)
        self.assertEqual(preview.request["tp"], 1.098)

    def test_hard_blocks_real_account(self) -> None:
        self.api.trade_mode = 2

        with self.assertRaisesRegex(RuntimeError, "hard-blocked"):
            prepare_demo_order(
                self.api,
                symbol="EURUSD",
                side=Side.LONG,
                stop_distance=0.001,
                settings=self.settings,
            )

        self.assertEqual(self.api.sent, [])

    def test_blocks_unexpected_login_and_existing_positions(self) -> None:
        self.api.login = 111111
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            prepare_demo_order(
                self.api,
                symbol="EURUSD",
                side=Side.LONG,
                stop_distance=0.001,
                settings=self.settings,
            )

        self.api.login = 123456
        self.api.positions = (object(),)
        with self.assertRaisesRegex(RuntimeError, "unmanaged account position"):
            prepare_demo_order(
                self.api,
                symbol="EURUSD",
                side=Side.LONG,
                stop_distance=0.001,
                settings=self.settings,
            )
        self.assertEqual(self.api.sent, [])

    def test_allows_multiple_symbols_within_portfolio_risk_cap(self) -> None:
        self.api.positions = (
            SimpleNamespace(
                ticket=101,
                symbol="EURUSD",
                type=self.api.ORDER_TYPE_BUY,
                volume=0.01,
                magic=self.settings.magic_number,
                sl=1.099,
                price_open=1.1001,
            ),
        )

        preview = prepare_demo_order(
            self.api,
            symbol="GBPUSD",
            side=Side.LONG,
            stop_distance=0.001,
            settings=self.settings,
        )

        self.assertEqual(preview.volume, 0.01)
        self.assertEqual(self.api.sent, [])

    def test_blocks_new_symbol_when_portfolio_open_risk_cap_is_used(self) -> None:
        settings = DemoOrderSettings(
            expected_account_id=123456,
            risk_per_trade=0.001,
            max_daily_loss=0.02,
            max_volume=0.01,
            max_spread=0.0003,
            max_total_open_risk=0.0001,
        )
        self.api.positions = (
            SimpleNamespace(
                ticket=101,
                symbol="EURUSD",
                type=self.api.ORDER_TYPE_BUY,
                volume=0.01,
                magic=settings.magic_number,
                sl=1.099,
                price_open=1.1001,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "portfolio open-risk cap"):
            prepare_demo_order(
                self.api,
                symbol="GBPUSD",
                side=Side.LONG,
                stop_distance=0.001,
                settings=settings,
            )

    def test_preview_is_allowed_when_terminal_trading_is_disabled(self) -> None:
        self.api.trading_allowed = False

        preview = prepare_demo_order(
            self.api,
            symbol="EURUSD",
            side=Side.LONG,
            stop_distance=0.001,
            settings=self.settings,
            require_trade_permission=False,
        )

        self.assertEqual(preview.volume, 0.01)
        self.assertEqual(self.api.sent, [])
        with self.assertRaisesRegex(RuntimeError, "enable Algo Trading"):
            prepare_demo_order(
                self.api,
                symbol="EURUSD",
                side=Side.LONG,
                stop_distance=0.001,
                settings=self.settings,
            )

    def test_blocks_daily_loss_and_excessive_spread(self) -> None:
        class LossApi(FakeTradingApi):
            def history_deals_get(self, date_from: datetime, date_to: datetime) -> tuple:
                return (
                    SimpleNamespace(
                        profit=-250.0,
                        commission=0.0,
                        swap=0.0,
                        fee=0.0,
                        entry=self.DEAL_ENTRY_OUT,
                    ),
                )

        loss_api = LossApi()
        with self.assertRaisesRegex(RuntimeError, "Daily loss limit"):
            prepare_demo_order(
                loss_api,
                symbol="EURUSD",
                side=Side.LONG,
                stop_distance=0.001,
                settings=self.settings,
            )

        self.api.quote = SimpleNamespace(bid=1.1000, ask=1.1010)
        with self.assertRaisesRegex(RuntimeError, "spread"):
            prepare_demo_order(
                self.api,
                symbol="EURUSD",
                side=Side.LONG,
                stop_distance=0.001,
                settings=self.settings,
            )
        self.assertEqual(self.api.sent, [])

    def test_submission_requires_opt_in_confirmation_and_is_idempotent(self) -> None:
        preview = prepare_demo_order(
            self.api,
            symbol="EURUSD",
            side=Side.SHORT,
            stop_distance=0.001,
            settings=self.settings,
        )
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "attempt.json"
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                submit_demo_order(
                    self.api,
                    preview,
                    settings=self.settings,
                    enabled=False,
                    confirmation="SUBMIT DEMO ORDER",
                    idempotency_file=marker,
                    signal_id="2026-01-01T00:00:00Z",
                )
            with self.assertRaisesRegex(RuntimeError, "confirmation"):
                submit_demo_order(
                    self.api,
                    preview,
                    settings=self.settings,
                    enabled=True,
                    confirmation="wrong",
                    idempotency_file=marker,
                    signal_id="2026-01-01T00:00:00Z",
                )
            self.assertEqual(self.api.sent, [])

            outcome = submit_demo_order(
                self.api,
                preview,
                settings=self.settings,
                enabled=True,
                confirmation="SUBMIT DEMO ORDER",
                idempotency_file=marker,
                signal_id="2026-01-01T00:00:00Z",
            )
            self.assertEqual(outcome.status, "FILLED")
            self.assertEqual(len(self.api.sent), 1)
            with self.assertRaisesRegex(RuntimeError, "already has an order-attempt"):
                submit_demo_order(
                    self.api,
                    preview,
                    settings=self.settings,
                    enabled=True,
                    confirmation="SUBMIT DEMO ORDER",
                    idempotency_file=marker,
                    signal_id="2026-01-01T00:00:00Z",
                )
            self.assertEqual(len(self.api.sent), 1)

    def test_closes_only_the_bot_managed_position_without_hedging(self) -> None:
        self.api.positions = (
            SimpleNamespace(
                ticket=101,
                symbol="EURUSD",
                type=self.api.ORDER_TYPE_BUY,
                volume=0.01,
                magic=self.settings.magic_number,
                sl=1.099,
                price_open=1.1001,
            ),
        )
        with TemporaryDirectory() as directory:
            outcome = close_demo_position(
                self.api,
                symbol="EURUSD",
                settings=self.settings,
                idempotency_file=Path(directory) / "close.json",
                signal_id="2026-01-01T00:00:00Z",
            )

        self.assertEqual(outcome.status, "FILLED")
        self.assertEqual(self.api.sent[0]["position"], 101)
        self.assertEqual(self.api.sent[0]["type"], self.api.ORDER_TYPE_SELL)

    def test_close_refuses_foreign_or_unprotected_position(self) -> None:
        self.api.positions = (
            SimpleNamespace(
                ticket=101,
                symbol="EURUSD",
                type=self.api.ORDER_TYPE_BUY,
                volume=0.01,
                magic=999,
                sl=1.099,
                price_open=1.1001,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "not managed"):
            close_demo_position(
                self.api,
                symbol="EURUSD",
                settings=self.settings,
                idempotency_file="unused.json",
                signal_id="signal",
            )

        self.api.positions = (
            SimpleNamespace(
                ticket=101,
                symbol="EURUSD",
                type=self.api.ORDER_TYPE_BUY,
                volume=0.01,
                magic=self.settings.magic_number,
                sl=0.0,
                price_open=1.1001,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "no protective stop"):
            close_demo_position(
                self.api,
                symbol="EURUSD",
                settings=self.settings,
                idempotency_file="unused.json",
                signal_id="signal",
            )

    def test_continuous_bot_waits_for_a_new_bar_before_entry(self) -> None:
        now = datetime.now(timezone.utc)
        bars = []
        for index in range(80):
            close = 1.0 + index * 0.001
            bars.append(
                Bar(
                    symbol="EURUSD",
                    timestamp=now - timedelta(minutes=15 * (80 - index)),
                    open=close,
                    high=close + 0.0001,
                    low=close - 0.0001,
                    close=close,
                )
            )
        args = SimpleNamespace(
            symbol="EURUSD",
            symbols=["EURUSD"],
            timeframe="M15",
            confirmation_timeframe="H1",
            lookback=20,
            atr_period=14,
            atr_multiplier=2.0,
        )

        with TemporaryDirectory() as directory:
            run_bar(
                self.api,
                bars=bars,
                confirmation_bars=bars,
                args=args,
                settings=self.settings,
                enter_allowed=False,
                neutral_wait_file=Path(directory) / "wait",
                active_position_file=Path(directory) / "active",
            )

        self.assertEqual(self.api.sent, [])

    def test_continuous_bot_opens_one_consensus_entry_on_new_bar(self) -> None:
        now = datetime.now(timezone.utc)
        bars = []
        for index in range(80):
            close = 1.0 + index * 0.001
            bars.append(
                Bar(
                    symbol="EURUSD",
                    timestamp=now - timedelta(minutes=15 * (80 - index)),
                    open=close,
                    high=close + 0.0001,
                    low=close - 0.0001,
                    close=close,
                )
            )
        args = SimpleNamespace(
            symbol="EURUSD",
            symbols=["EURUSD"],
            timeframe="M15",
            confirmation_timeframe="H1",
            lookback=20,
            atr_period=14,
            atr_multiplier=2.0,
        )

        with TemporaryDirectory() as directory:
            run_bar(
                self.api,
                bars=bars,
                confirmation_bars=bars,
                args=args,
                settings=self.settings,
                enter_allowed=True,
                neutral_wait_file=Path(directory) / "wait",
                active_position_file=Path(directory) / "active",
            )

        self.assertEqual(len(self.api.sent), 1)
        self.assertGreater(self.api.sent[0]["sl"], 0)
        self.assertEqual(self.api.sent[0]["volume"], 0.01)

    def test_news_guard_blocks_entry_without_blocking_existing_order_management(self) -> None:
        now = datetime.now(timezone.utc)
        bars = []
        for index in range(80):
            close = 1.0 + index * 0.001
            bars.append(
                Bar(
                    symbol="EURUSD",
                    timestamp=now - timedelta(minutes=15 * (80 - index)),
                    open=close,
                    high=close + 0.0001,
                    low=close - 0.0001,
                    close=close,
                )
            )
        args = SimpleNamespace(
            symbol="EURUSD",
            symbols=["EURUSD"],
            timeframe="M15",
            confirmation_timeframe="H1",
            lookback=20,
            atr_period=14,
            atr_multiplier=2.0,
        )

        with TemporaryDirectory() as directory:
            run_bar(
                self.api,
                bars=bars,
                confirmation_bars=bars,
                args=args,
                settings=self.settings,
                enter_allowed=True,
                entry_block_reason="high-impact event",
                neutral_wait_file=Path(directory) / "wait",
                active_position_file=Path(directory) / "active",
            )

        self.assertEqual(self.api.sent, [])

    def test_higher_timeframe_opposition_blocks_entry(self) -> None:
        now = datetime.now(timezone.utc)
        lower_bars = []
        higher_bars = []
        for index in range(80):
            lower_close = 1.0 + index * 0.001
            higher_close = 1.2 - index * 0.001
            timestamp = now - timedelta(minutes=5 * (80 - index))
            lower_bars.append(
                Bar(
                    symbol="EURUSD",
                    timestamp=timestamp,
                    open=lower_close,
                    high=lower_close + 0.0001,
                    low=lower_close - 0.0001,
                    close=lower_close,
                )
            )
            higher_bars.append(
                Bar(
                    symbol="EURUSD",
                    timestamp=timestamp,
                    open=higher_close,
                    high=higher_close + 0.0001,
                    low=higher_close - 0.0001,
                    close=higher_close,
                )
            )
        args = SimpleNamespace(
            symbol="EURUSD",
            symbols=["EURUSD"],
            timeframe="M5",
            confirmation_timeframe="M15",
            lookback=20,
            atr_period=14,
            atr_multiplier=2.0,
        )

        with TemporaryDirectory() as directory:
            run_bar(
                self.api,
                bars=lower_bars,
                confirmation_bars=higher_bars,
                args=args,
                settings=self.settings,
                enter_allowed=True,
                neutral_wait_file=Path(directory) / "wait",
                active_position_file=Path(directory) / "active",
            )

        self.assertEqual(self.api.sent, [])

    def test_bot_waits_for_neutral_consensus_after_active_position_disappears(self) -> None:
        now = datetime.now(timezone.utc)
        bars = []
        for index in range(80):
            close = 1.0 + index * 0.001
            bars.append(
                Bar(
                    symbol="EURUSD",
                    timestamp=now - timedelta(minutes=15 * (80 - index)),
                    open=close,
                    high=close + 0.0001,
                    low=close - 0.0001,
                    close=close,
                )
            )
        args = SimpleNamespace(
            symbol="EURUSD",
            symbols=["EURUSD"],
            timeframe="M15",
            confirmation_timeframe="H1",
            lookback=20,
            atr_period=14,
            atr_multiplier=2.0,
        )
        with TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            neutral_wait = Path(directory) / "wait"
            active.write_text("previously active\n", encoding="utf-8")

            run_bar(
                self.api,
                bars=bars,
                confirmation_bars=bars,
                args=args,
                settings=self.settings,
                enter_allowed=True,
                neutral_wait_file=neutral_wait,
                active_position_file=active,
            )

            self.assertTrue(neutral_wait.exists())
            self.assertFalse(active.exists())
            self.assertEqual(self.api.sent, [])

    def test_bot_clears_reentry_latch_only_after_neutral_consensus(self) -> None:
        now = datetime.now(timezone.utc)
        bars = [
            Bar(
                symbol="EURUSD",
                timestamp=now - timedelta(minutes=15 * (80 - index)),
                open=1.1,
                high=1.1,
                low=1.1,
                close=1.1,
            )
            for index in range(80)
        ]
        args = SimpleNamespace(
            symbol="EURUSD",
            symbols=["EURUSD"],
            timeframe="M15",
            confirmation_timeframe="H1",
            lookback=20,
            atr_period=14,
            atr_multiplier=2.0,
        )
        with TemporaryDirectory() as directory:
            neutral_wait = Path(directory) / "wait"
            neutral_wait.write_text("wait\n", encoding="utf-8")

            run_bar(
                self.api,
                bars=bars,
                confirmation_bars=bars,
                args=args,
                settings=self.settings,
                enter_allowed=True,
                neutral_wait_file=neutral_wait,
                active_position_file=Path(directory) / "active",
            )

            self.assertFalse(neutral_wait.exists())
            self.assertEqual(self.api.sent, [])

    def test_rechecks_demo_account_immediately_before_send(self) -> None:
        class AccountSwitchApi(FakeTradingApi):
            def order_check(self, request: dict[str, object]) -> SimpleNamespace:
                result = super().order_check(request)
                self.trade_mode = 2
                return result

        api = AccountSwitchApi()
        settings = self.settings
        preview = prepare_demo_order(
            api,
            symbol="EURUSD",
            side=Side.LONG,
            stop_distance=0.001,
            settings=settings,
        )
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "attempt.json"
            with self.assertRaisesRegex(RuntimeError, "hard-blocked"):
                submit_demo_order(
                    api,
                    preview,
                    settings=settings,
                    enabled=True,
                    confirmation="SUBMIT DEMO ORDER",
                    idempotency_file=marker,
                    signal_id="2026-01-01T00:00:00Z",
                )
        self.assertEqual(api.sent, [])

    def test_rejects_risk_configuration_above_demo_cap(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most"):
            DemoOrderSettings(expected_account_id=123456, risk_per_trade=0.01)

    def test_rejects_unreasonable_reward_risk_ratio(self) -> None:
        with self.assertRaisesRegex(ValueError, "reward_risk_ratio"):
            DemoOrderSettings(expected_account_id=123456, reward_risk_ratio=0)
