from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from fxbot.dashboard import DemoProfile
from fxbot.portfolio import snapshot_profile


class FakeTerminal:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_INOUT = 2
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self, *, login=123456, mode=0, initialize_ok=True):
        self.login = login
        self.mode = mode
        self.initialize_ok = initialize_ok
        self.initialized_path = None
        self.shutdown_called = False

    def initialize(self, *, path, portable, timeout):
        self.initialized_path = path
        self.portable = portable
        return self.initialize_ok

    def shutdown(self):
        self.shutdown_called = True

    def account_info(self):
        return SimpleNamespace(
            login=self.login,
            trade_mode=self.mode,
            balance=10000.0,
            equity=9900.0,
            margin=500.0,
            margin_free=9400.0,
            margin_level=1980.0,
            currency="USD",
        )

    def positions_get(self):
        return (object(),)

    def history_deals_get(self, start, end):
        return (
            SimpleNamespace(entry=0, profit=0, commission=-1, swap=0, fee=0),
            SimpleNamespace(entry=1, profit=25, commission=-1, swap=0, fee=0),
        )

    def last_error(self):
        return (0, "Success")


def _profile(terminal_path):
    return DemoProfile.from_payload(
        {
            "name": "Demo",
            "account_id": 123456,
            "symbols": ["EURUSD"],
            "max_spreads": {"EURUSD": 0.0003},
            "terminal_path": terminal_path,
        }
    )


class PortfolioSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.terminal_exe = Path(self.temp_dir.name) / "terminal64.exe"
        self.terminal_exe.write_text("test", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reads_metrics_only_after_expected_demo_login_verifies(self):
        api = FakeTerminal()
        result = snapshot_profile(_profile(str(self.terminal_exe)), api=api)

        self.assertTrue(result.verified)
        self.assertEqual(result.balance, 10000.0)
        self.assertEqual(result.equity, 9900.0)
        self.assertEqual(result.margin, 500.0)
        self.assertEqual(result.open_position_count, 1)
        self.assertEqual(result.closed_deals_today, 1)
        self.assertEqual(result.realized_pnl_today, 24.0)
        self.assertTrue(api.shutdown_called)
        self.assertEqual(api.initialized_path, str(self.terminal_exe))
        self.assertTrue(api.portable)

    def test_wrong_login_withholds_sensitive_metrics_and_shuts_down(self):
        api = FakeTerminal(login=222222)
        result = snapshot_profile(_profile(str(self.terminal_exe)), api=api)

        self.assertFalse(result.verified)
        self.assertIsNone(result.balance)
        self.assertIn("does not match", result.error)
        self.assertTrue(api.shutdown_called)

    def test_live_account_withholds_metrics(self):
        api = FakeTerminal(mode=FakeTerminal.ACCOUNT_TRADE_MODE_REAL)
        result = snapshot_profile(_profile(str(self.terminal_exe)), api=api)

        self.assertFalse(result.verified)
        self.assertIsNone(result.equity)
        self.assertIn("not logged in to a demo", result.error)

    def test_unconfigured_terminal_fails_closed(self):
        result = snapshot_profile(_profile(terminal_path=""), api=FakeTerminal())

        self.assertFalse(result.verified)
        self.assertIsNone(result.balance)
        self.assertIn("Configure a separate", result.error)


if __name__ == "__main__":
    unittest.main()
