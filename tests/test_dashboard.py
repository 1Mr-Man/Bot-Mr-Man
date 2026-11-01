import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fxbot.dashboard import (
    BotController,
    DashboardServer,
    DemoProfile,
    ProfileStore,
)


def profile_payload(**updates):
    payload = {
        "name": "Demo A",
        "account_id": 10013250306,
        "symbols": ["GBPUSD", "EURUSD"],
        "max_spreads": {"GBPUSD": 0.0003, "EURUSD": 0.0003},
        "max_open_positions": 1,
    }
    payload.update(updates)
    return payload


class ProfileStoreTests(unittest.TestCase):
    def test_validates_demo_profile_and_persists_without_passwords(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = ProfileStore(path)
            profile = store.save(profile_payload())

            loaded = store.get(profile.id)
            saved_json = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded.account_id, 10013250306)
        self.assertEqual(loaded.symbols, ("GBPUSD", "EURUSD"))
        self.assertEqual(loaded.strategy_mode, "consensus")
        self.assertEqual(loaded.selected_strategy, "sma")
        self.assertEqual(loaded.enabled_strategies, ("sma", "donchian", "ema", "macd", "rsi"))
        self.assertEqual(loaded.consensus_votes, 3)
        self.assertNotIn("password", json.dumps(saved_json).lower())

    def test_profiles_persist_independent_strategy_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            profile_a = store.save(
                profile_payload(
                    strategy_mode="single",
                    selected_strategy="macd",
                    enabled_strategies=["macd"],
                    consensus_votes=1,
                    terminal_path=r"C:\\MT5-A\\terminal64.exe",
                )
            )
            profile_b = store.save(
                profile_payload(
                    name="Demo B",
                    account_id=20000002,
                    strategy_mode="consensus",
                    enabled_strategies=["sma", "donchian", "ema"],
                    consensus_votes=2,
                )
            )
            loaded_a = store.get(profile_a.id)
            loaded_b = store.get(profile_b.id)

        self.assertEqual(loaded_a.selected_strategy, "macd")
        self.assertEqual(loaded_a.terminal_path, r"C:\\MT5-A\\terminal64.exe")
        self.assertEqual(loaded_b.enabled_strategies, ("sma", "donchian", "ema"))
        self.assertEqual(loaded_b.consensus_votes, 2)

    def test_rejects_real_account_like_or_invalid_limits(self) -> None:
        for invalid in (
            profile_payload(account_id=0),
            profile_payload(max_total_open_risk=0.02),
            profile_payload(max_spreads={"GBPUSD": 0.0003}),
            profile_payload(symbols=["EURUSD", "eurusd"]),
            profile_payload(symbols=["GBPUSD"], max_spreads={"GBPUSD": 0.0003}, max_open_positions=2),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    DemoProfile.from_payload(invalid)

    def test_limits_profiles_and_duplicate_logins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            store.save(profile_payload())
            with self.assertRaisesRegex(ValueError, "different MT5 demo login"):
                store.save(profile_payload(name="Other"))
            for index in range(2, 6):
                store.save(
                    profile_payload(
                        name=f"Demo {index}",
                        account_id=10013250306 + index,
                    )
                )
            with self.assertRaisesRegex(ValueError, "Maximum of 5"):
                store.save(
                    profile_payload(name="Sixth", account_id=20000006)
                )


class BotControllerTests(unittest.TestCase):
    def test_launches_only_guarded_demo_runner_with_saved_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "examples").mkdir()
            (root / "examples" / "mt5_demo_bot.py").write_text("", encoding="utf-8")
            store = ProfileStore(root / "state" / "profiles.json")
            profile = store.save(profile_payload())
            controller = BotController(root, root / "state", store)
            fake_process = unittest.mock.Mock()
            fake_process.pid = 1234
            fake_process.poll.return_value = None
            with patch("fxbot.dashboard.subprocess.Popen", return_value=fake_process) as popen:
                status = controller.start(profile.id)

            command = popen.call_args.args[0]
            self.assertIn("--run-demo-bot", command)
            self.assertIn("RUN DEMO BOT", command)
            self.assertIn("10013250306", command)
            self.assertIn("--strategy-mode", command)
            self.assertIn("consensus", command)
            self.assertIn("--strategies", command)
            self.assertIn("sma,donchian,ema,macd,rsi", command)
            self.assertIn("--consensus-votes", command)
            self.assertFalse(popen.call_args.kwargs["shell"])
            self.assertTrue(status["running"])
            with self.assertRaisesRegex(ValueError, "already running"):
                controller.start(profile.id)

    def test_stop_requests_kill_switch_instead_of_killing_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "examples").mkdir()
            (root / "examples" / "mt5_demo_bot.py").write_text("", encoding="utf-8")
            store = ProfileStore(root / "state" / "profiles.json")
            profile = store.save(profile_payload())
            controller = BotController(root, root / "state", store)
            fake_process = unittest.mock.Mock()
            fake_process.pid = 2345
            fake_process.poll.return_value = None
            with patch("fxbot.dashboard.subprocess.Popen", return_value=fake_process):
                controller.start(profile.id)
                stopped = controller.stop()

            self.assertTrue(stopped["stop_requested"])
            fake_process.kill.assert_not_called()


class DashboardHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.server = DashboardServer(
            project_root=root,
            state_dir=root / "state",
            port=18765,
            token="test-dashboard-token",
        )
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:18765"

    def tearDown(self) -> None:
        self.server.httpd.shutdown()
        self.server.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_binds_only_loopback_and_requires_token_for_status(self) -> None:
        with urlopen(self.base + "/") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/api/status")
        self.assertEqual(error.exception.code, 401)
        request = Request(
            self.base + "/api/status",
            headers={"Authorization": "Bearer test-dashboard-token"},
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read())
        self.assertEqual(data["profiles"], [])

    def test_mutations_require_token_and_same_origin(self) -> None:
        body = json.dumps(profile_payload()).encode()
        request = Request(
            self.base + "/api/profiles",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-dashboard-token",
                "Origin": "http://evil.invalid",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
