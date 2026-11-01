from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
from tempfile import TemporaryDirectory

from fxbot.dashboard import DashboardServer, ProfileStore
from tests.test_dashboard import profile_payload
from tests.test_portfolio import FakeTerminal


class PortfolioDashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        store = ProfileStore(base / "state" / "profiles.json")
        self.terminal_dir = base / "mt5-terminal"
        self.terminal_dir.mkdir()
        self.terminal_path = self.terminal_dir / "terminal64.exe"
        self.terminal_path.write_text("test", encoding="utf-8")
        self.profile = store.save(
            profile_payload(
                symbols=["EURUSD"],
                max_spreads={"EURUSD": 0.0003},
                terminal_path=str(self.terminal_path),
                initial_balance=20,
            )
        )
        self.api = FakeTerminal(login=10013250306)
        self.server = DashboardServer(
            project_root=base,
            state_dir=base / "state",
            port=18766,
            token="test-token",
            api_factory=lambda: self.api,
        )
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:18766"

    def tearDown(self):
        self.server.httpd.shutdown()
        self.server.httpd.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, path, body=None, method="GET"):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": "Bearer test-token"}
        if data is not None:
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Origin": self.base_url,
                }
            )
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        with urlopen(request) as response:
            return json.loads(response.read())

    def test_status_includes_separately_verified_metrics(self):
        self.server.controller.store = ProfileStore(self.server.controller.store.path)
        self.server.controller.store.save(
            {
                "id": self.profile.id,
                "name": self.profile.name,
                "account_id": self.profile.account_id,
                "symbols": list(self.profile.symbols),
                "max_spreads": self.profile.max_spreads,
                "terminal_path": str(self.terminal_path),
                "backtest_start": "2025-01-01",
                "backtest_end": "2025-01-31",
                "initial_balance": 20,
                "test_stop_distance": 0.001,
                "test_spread": 0.0003,
            }
        )
        data = self.request("/api/status")

        self.assertTrue(data["accounts"][0]["verified"], data["accounts"][0])
        self.assertEqual(data["accounts"][0]["equity"], 9900.0)
        self.assertTrue(self.api.shutdown_called)

    def test_stage_endpoint_matches_growth_formula(self):
        data = self.request(f"/api/stages/{self.profile.id}")

        self.assertAlmostEqual(data["targets"][9]["target_balance"], 275.7169836980)
        self.assertAlmostEqual(data["targets"][29]["target_balance"], 52399.9128729990)

    def test_backtest_route_uses_selected_profile_and_no_order_api(self):
        bars = []
        for index in range(80):
            close = 1.0 + index * 0.001
            bars.append(
                __import__("fxbot.models", fromlist=["Bar"]).Bar(
                    symbol="EURUSD",
                    timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc)
                    + __import__("datetime").timedelta(hours=index),
                    open=close,
                    high=close + 0.0001,
                    low=close - 0.0001,
                    close=close,
                )
            )
        with patch("fxbot.dashboard.Mt5BarSource.load", return_value=bars):
            result = self.request(
                "/api/backtest",
                {
                    "profile_id": self.profile.id,
                    "symbol": "EURUSD",
                    "timeframe": "H1",
                    "start": "2025-01-01",
                    "end": "2025-01-31",
                    "initial_balance": 20,
                    "stop_distance": 0.001,
                    "spread": 0.0003,
                },
                method="POST",
            )["simulation"]

        self.assertEqual(result["initial_balance"], 20)
        self.assertEqual(result["bars"], 80)
        self.assertIsInstance(result["closed_trades"], int)
        self.assertIn("trades_to_stage_30", result)
        self.assertIn("does not establish future reliability", result["caveat"])


if __name__ == "__main__":
    unittest.main()
