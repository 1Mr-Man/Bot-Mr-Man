import argparse
import secrets
import threading
import webbrowser
from pathlib import Path

from fxbot.dashboard import DashboardServer, default_state_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the localhost-only FX Bot demo dashboard."
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--state-dir",
        help="Local data folder (default: %%LOCALAPPDATA%%\\FXBot)",
    )
    args = parser.parse_args()
    state_dir = default_state_dir() if not args.state_dir else Path(args.state_dir)
    token = secrets.token_urlsafe(32)
    server = DashboardServer(
        project_root=Path(__file__).resolve().parents[1],
        state_dir=state_dir,
        port=args.port,
        token=token,
    )
    url = f"http://127.0.0.1:{args.port}/#token={token}"
    print("FX Bot dashboard is local-only; remote access is not enabled.")
    print(f"Open this private URL in your browser: {url}")
    print(f"Local profile/log storage: {state_dir}")
    print("Demo order execution can occur after you press Start; stop with Ctrl+C.")
    timer = threading.Timer(0.5, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard and requesting worker stop…")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
