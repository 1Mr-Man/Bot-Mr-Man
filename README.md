# FX Bot

A paper-first foundation for a modular Forex trading bot. The initial strategy
is a transparent moving-average trend follower; additional strategies can be
added behind the same `Strategy` interface.

## Current scope

- Pure Python core; no broker connection or live order capability.
- OHLC bar model with input validation.
- Five rule-based strategy candidates: SMA trend, Donchian breakout, EMA trend,
  MACD trend, and RSI mean reversion.
- Paper-account simulation with next-bar-open fills, configurable spread,
  slippage and commission, stop handling, and risk-sized positions.
- Daily loss lockout based on realized PnL per UTC calendar day.
- Signal-only replay for inspecting target-position changes separately.
- Backtest run cards with returns, max drawdown, trade statistics, cost/risk
  assumptions, warnings, and JSON export.
- Unit tests for strategy warm-up, execution timing, risk guards, and analytics.

This is a development starting point, not a validated trading system. The
example strategy is illustrative and makes no claim of profitability. The
simulator is a simplified single-position model; it does not handle margin,
financing/swaps, broker-specific sizing, or live data. Stops may be exceeded
when a bar gaps through the stop. EUR/USD results assume USD account-value
conversion (`value_per_price_unit=1`); other instruments need the correct
conversion configured. MT5 demo execution tools are documented below and
hard-block real accounts.

## Run

From this directory, with Python 3.11 or newer:

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
python examples/synthetic_eurusd.py
```

The code uses only the Python standard library. No API keys or trading account
are needed. The example uses synthetic EUR/USD 1-hour bars, not live or
historical market data.

The backtest analytics can be used directly:

```python
from fxbot.analytics import build_run_card

card = build_run_card(result, bars, config, strategy_name="SMA trend")
card.write_json("runs/sma-trend/run-card.json")
```

Metrics are intentionally limited: profit factor is `null` when no losing
trade exists, and trade-based statistics are `null` when no trades close.
Drawdown uses the simulated bar-by-bar equity marks, including open-position
mark-to-market but not hypothetical liquidation costs.

## Local dashboard (demo only)

A localhost-only dashboard can store up to five separate demo-account profiles,
show a guarded worker's output, and request that one continuous demo worker
start or stop. Each profile can use one selected built-in strategy or a
configurable consensus of selected rules and a vote threshold. The initial
combined default enables SMA 10/30, Donchian 20, EMA 12/26, MACD 12/26/9, and
RSI 14 with 3-of-5 votes; single-rule mode defaults to SMA 10/30. Remote access,
live accounts, and copied-signal execution are not included. It writes profile data under
`%LOCALAPPDATA%\\FXBot` by default; no MT5 passwords are stored. The dashboard
never binds to a public network interface and requires a random token each
launch.

Install the existing MT5 dependency and start the dashboard from PowerShell:

```powershell
python -m pip install -e ".[mt5]"
python examples/fxbot_dashboard.py
```

Open the `http://127.0.0.1:.../#token=...` URL printed by the program. Keep it
private; do not publish it or expose dashboard port 8765. Add each MT5 **demo**
login with the exact broker symbol names and per-symbol spread caps. Start
submits entries automatically when the continuous bot's strategy, confirmation,
and safety checks all pass. Stop requests graceful shutdown; protective
broker-side SL/TP orders remain attached. Run one continuous bot worker at a
time. Existing wait markers and order-attempt records stay in the project
`runs\\` folder and should not be deleted to force an entry. Each profile
requires its own absolute MT5 terminal executable path for account monitoring.
The dashboard opens each path serially in MT5 portable mode, verifies the
reported login and demo status, and only then displays balance, equity, margin,
positions, and today's closed-deal PnL. Choose a terminal executable in a
separate portable data directory for every profile; reusing a standard shared
terminal-data directory may cause the login verification to fail closed. It
closes each MT5 connection after the snapshot.
 The continuous trading worker is still limited to one activeaccount and uses the normally connected terminal; profile monitor paths are not
yet used to route orders. Treat every configured account as demo-only and verify
its login in MT5 before pressing Start. This milestone has not been packaged as
a Windows installer.

The dashboard also offers a historical paper simulation for the profile's
selected strategy and symbol over a chosen stored date range. Configure the
initial simulation balance (default $10,000), 30-stage count, and growth rate
(default 30%) on the profile. It reports closed trade count, win rate, drawdown,
final simulated equity, and which stage target that historical result reached.
It uses the simplified simulator and the profile's stop/spread assumptions. It
does not establish future reliability or extrapolate a number of trades to
reach stage 30. `examples/fxbot_stage_backtest.py` runs the same type of
simulation directly from PowerShell and can save a JSON report.

Stage targets use `initial_balance × (1 + 0.30)^n` by default (growth rate and
stage count are configurable). For $20, stage 10 is approximately $275.72 and
stage 30 approximately $52,399.91. These are arithmetic milestones, not
forecasts or guarantees.
## Read-only MetaTrader 5 history

On Windows, install the optional adapter, start MetaTrader 5, and log in to a
demo or contest account. The adapter rejects real accounts and only reads
historical rates; it has no order-submission functions. EUR/USD H1 remains the
default, but M5, M15, M30, H1, and H4 are supported. The symbol can be changed to a
broker's gold symbol such as `XAUUSD` or `GOLD`.

```powershell
python -m pip install -e ".[mt5]"
python examples/mt5_paper_run.py `
  --start 2025-01-01 --end 2025-01-31 `
  --stop-distance 0.0010 --spread 0.0001
```

For a gold M15 historical simulation, provide a stop distance and spread in
your broker's gold price units:

```powershell
python examples/mt5_paper_run.py `
  --symbol XAUUSD --timeframe M15 `
  --start 2025-01-01 --end 2025-01-31 `
  --stop-distance 5.0 --spread 0.30
```

The example values are not a recommendation. Paper-backtest position sizing
is simplified and does not fetch the broker's gold contract specification;
use MT5 preview sizing for broker-aware demo estimates.

To run the Donchian breakout on the same period:

```powershell
python examples/mt5_paper_run.py `
  --start 2025-01-01 --end 2025-01-31 `
  --stop-distance 0.0010 --spread 0.0001 `
  --strategy donchian --lookback 20
```

Dates are UTC and `--end` is inclusive. Broker symbol aliases may differ from
`EURUSD`; pass the terminal's exact name with `--symbol`. Set spread,
slippage, commission, and stop distance to the assumptions you want to test.
The adapter excludes the currently forming H1 bar; the resulting run is still
a simplified historical simulation, not forward paper trading through the
broker. Each run saves a JSON report under `runs\` by default. Use `--report`
to choose a different file path. The report includes performance metrics,
assumptions, warnings, and any open position.

## Signal preview and guarded demo entries

The signal runner evaluates the latest completed bar. By default it uses EUR/USD
H1, but can be configured for M15 gold. Unless explicitly opted in, it only
prints a new rule-based entry signal and a demo order preview; it does not
submit an order. Preview works even when MT5 Algo Trading is turned off:

```powershell
python examples/mt5_demo_signal.py
```

To preview gold on M15, replace `XAUUSD` with your broker's exact gold symbol
if needed. The stop defaults to 2 times the recent 14-bar ATR; spread cap is
0.50 price units for recognized gold symbols. Broker spreads and gold contracts
vary, so check those settings before any demo execution:

```powershell
python examples/mt5_demo_signal.py `
  --symbol XAUUSD --timeframe M15 --strategy donchian --lookback 20
```

To scan all five strategies against the same latest completed bars, use
`--strategy all`. The scan reports each strategy's current target (new long,
new short, hold, exit, or no signal) and never submits an order:

```powershell
python examples/mt5_demo_signal.py `
  --symbol XAUUSD --timeframe M15 --strategy all
```

To scan several MT5 symbols in one read-only command, pass a comma-separated
list with `--symbols`. Use the exact symbol names shown by your broker in
Market Watch; symbols unavailable from the terminal are reported individually:

```powershell
python examples/mt5_demo_signal.py `
  --symbols XAUUSD,EURUSD,GBPUSD --timeframe M5 --strategy all
```

Multi-symbol signal scans are always read-only and require `--strategy all`;
they do not place orders.

The five configurable candidates are SMA 10/30 trend, Donchian breakout,
EMA 12/26 trend, MACD 12/26/9 trend, and RSI 14 mean reversion (30/70
thresholds). These are common, transparent examples—not objectively "best"
strategies or evidence of profitability. They can disagree; the all-strategy
scan does not vote or combine them into an order recommendation. Backtest a
candidate on your symbol and timeframe, include realistic costs, and test it
out of sample before considering demo use.

The signal runner supports `M5`, `M15`, `M30`, and `H1`; select one individual
strategy with `--strategy donchian` (default), `sma`, `ema`, `macd`, or `rsi`.
The historical paper runner also accepts those five strategies. Any submitted
entry is restricted in code to an MT5 demo account, the exact login supplied
with `--expected-account-id`, and a single account position. It requires a
protective stop, rejects stale signals and excessive spreads, caps position
risk at 0.25% (default 0.1%), applies a daily realized-loss lockout, and stores
an idempotency record before submission. Existing account positions block new
entries. This one-shot runner does not manage or close positions. Use the
separate continuous demo runner below only if you want its 3-of-5 exit rules.
The `runs\DEMO_STOP` file blocks order submission; create it to engage the
local kill switch. Remove it only when you intentionally want to re-enable
demo execution.

### One-shot consensus demo order (advanced)

`examples/mt5_demo_consensus_once.py` makes at most one demo order attempt. It
requires a fresh 3-of-5 consensus on the trading timeframe and matching 3-of-5
signal plus 3-of-4 trend votes on the higher timeframe. It then applies the
same demo-account, daily-loss, spread, position, and portfolio-risk checks as
the other guarded MT5 executor. It is not a continuous runner. The confirmation
string explicitly authorizes an order attempt; if every preflight passes, the
order may be submitted:

```powershell
python examples/mt5_demo_consensus_once.py `
  --symbol GBPUSD --timeframe M5 --confirmation-timeframe M15 `
  --expected-account-id 12345678 --max-spread 0.0003 `
  --confirm "SUBMIT ONE DEMO ORDER"
```

Replace the example login with the exact MT5 demo login and set a spread cap
appropriate for the broker. Do not use this command on a real account. An
existing same-symbol position, stale or opposing signals, or any failed
preflight prevents submission. A signal is not a profitability guarantee.

### Continuous demo bot (advanced; use a demo account only)

An optional continuously running runner uses the five-strategy consensus:
enter when at least 3 of 5 current strategy states agree, and close the bot's
own position when at least 3 of 5 signal the opposite direction. After a close
or detected stop-out, it waits for the five strategies to return to neutral
before considering another entry, and then waits for a subsequent completed
bar. It evaluates completed bars only, allows up to 10 bot-managed positions
across unique configured symbols by default, and exits by sending a market
close for that exact position ticket. Every entry
has a broker-side ATR-based stop; the position retains that stop if the
program stops. Each entry also receives a broker-side take-profit by default
at 2 times the stop distance (2:1 reward-to-risk). This is a target, not a
guaranteed return; execution costs and slippage affect the realized ratio. The
runner cannot close a manual position or any position with another magic number.

For entries, the bot also requires its higher-timeframe 3-of-5 signal and
3-of-4 trend vote to agree with the lower-timeframe direction. Defaults pair
M5 with M15, M15/M30 with H1, and H1 with H4. Override with
`--confirmation-timeframe` if desired; it must be higher than the trading
timeframe. Stale or directionally conflicting confirmation blocks new entries.
The higher-timeframe gate does not delay stop-losses or consensus exits.

The guarded defaults are 0.1% account-equity risk per entry, 2% maximum daily
realized loss, 0.01 maximum lots, a 2 x 14-bar ATR stop, 2:1 reward-to-risk,
and a 0.50 gold spread cap in gold price units. It can scan multiple symbols
exposed by the connected MT5 broker with `--symbols XAUUSD,EURUSD,GBPUSD`.
The configured list is limited to 50 symbols; by default it allows at most 10
open bot positions, one per symbol, and limits aggregate stop-loss risk to 1%
of equity. Unmanaged account positions still block new entries. This does not
provide separate crypto or stock venue integrations; those remain paper-only
until their broker/exchange is selected. The continuous runner defaults to
M5; choose M15 instead if you want fewer, slower checks. Broker contract
sizes, spreads, and fills vary. Check the symbol's MT5 specification and
replace `XAUUSD` or the spread cap as needed. These values are guardrails, not
a profitability claim or a recommendation to trade.

#### Optional economic calendar and news context

The continuous runner can use Finnhub's economic calendar to block **new**
entries 30 minutes before through 15 minutes after a high-impact event in the
symbol's mapped currencies. Existing positions can still exit. General market
headlines are printed as context only; they do not create a directional signal.
The guard is opt-in. When enabled, missing credentials, provider errors,
unsupported currency mappings, or malformed calendar data block new entries
rather than being treated as an empty calendar. Calendar and headline responses
are cached for 15 minutes.

Create a Finnhub API key in your own account and set it in the current
PowerShell window (do not paste the key into chat or commit it):

```powershell
$env:FINNHUB_API_KEY = "your-key"
```

Then add `--news-guard` to the continuous-runner command. Only the economic
calendar and general headlines are currently integrated; company fundamentals
and earnings are not yet used, and stocks/crypto are not connected to an
execution venue.

To test Finnhub without connecting to MT5 or submitting orders, run:

```powershell
python examples/finnhub_news_check.py
```

This read-only check fetches and prints upcoming high-impact calendar events
and general headlines. It never initializes the trading adapter.
If Finnhub returns HTTP 403, verify the API key is active and that the account's
plan permits the endpoint named in the error (the calendar is requested before
headlines). The key is never printed by the checker.
The Finnhub API schema identifies `/calendar/economic` as requiring premium
access, so a 403 on that endpoint may reflect the subscription tier rather
than an incorrectly entered key. The read-only checker now tests calendar and
headlines independently, so a calendar denial does not prevent the separate
headlines endpoint from being checked. If calendar access is unavailable, do
not enable `--news-guard`; it intentionally blocks new entries when its
calendar data cannot be read.

For multiple symbols, provide broker-specific spread limits for each instrument.
For example, the values below are illustrative only and must be checked against
your broker:

The bot does not run in this setup session. To arm it yourself, first verify
that MT5 is logged into the intended **demo** account and copy that account's
login number, and enable Algo Trading in MT5. Replace `12345678` with that
demo login, check that `0.50` is appropriate for your broker's XAUUSD spread,
then run:

```powershell
python examples/mt5_demo_bot.py `
  --symbols XAUUSD,EURUSD,GBPUSD --timeframe M5 `
  --expected-account-id 12345678 `
  --max-spreads XAUUSD=0.50,EURUSD=0.0003,GBPUSD=0.0003 `
  --max-open-positions 3 --max-total-open-risk 0.01 `
  --reward-risk-ratio 2.0 `
  --news-guard --run-demo-bot --confirm "RUN DEMO BOT"
```

This command can submit and close demo trades without asking again for each
trade. It is hard-blocked on real accounts and stops on foreign positions,
failed/partial executions, or ambiguous outcomes. Stop it with `Ctrl+C`, or
create the kill-switch file from another PowerShell window:

```powershell
New-Item -ItemType Directory -Path .\runs -Force | Out-Null
New-Item -ItemType File -Path .\runs\DEMO_STOP -Force | Out-Null
```

An open position keeps its broker stop-loss. Persistent state markers in
`runs\` prevent automatic re-entry after a close or detected stop-out until
consensus neutralizes.
Inspect the account and attempt records before manually restarting after an
execution error. Never use this runner on an account containing money.
If MT5 data or terminal authorization temporarily fails, the runner reports
the issue, places no order, and retries after 30 seconds. Stale bars are
reported explicitly and never trigger an entry; verify MT5 is logged in to the
intended demo server and the symbol is receiving quotes.

Only if you deliberately want to place one order on the **demo** account, use
the exact login shown in MetaTrader 5 and explicit confirmation:

```powershell
python examples/mt5_demo_signal.py `
  --execute-demo `
  --expected-account-id 12345678 `
  --confirm "SUBMIT DEMO ORDER"
```

Replace `12345678` with your MT5 demo login. This command can submit one demo
market entry when a fresh signal exists; never use it with an account that
contains money. The program refuses real accounts even if the opt-in flags are
provided. A same-signal retry is blocked; inspect the attempt record and
terminal before taking any manual recovery action. Demo execution has only
been tested with mocked MT5 responses, not by submitting a broker order.

## Design direction

The strategy contract is separate from paper execution, so strategies do not
have broker access. Next steps are broker-appropriate trading-calendar
handling, systematic strategy comparisons, and out-of-sample evaluation.
Real-account execution is not implemented.
