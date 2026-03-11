# Cross-Venue Momentum Trading System

A Python 3.11+ asyncio system that monitors Uniswap V3 swap events across three fee-tier pools (30 bps, 5 bps, 1 bps) on Arbitrum, constructs directional momentum signals, and places maker limit orders on Deribit perpetual futures.

## Prerequisites

- Python 3.11 or later
- An Arbitrum RPC WebSocket endpoint (e.g. Alchemy, Infura)
- A Deribit account with API credentials (for live/paper modes)

## Installation

```bash
cd momentum_system
pip install -r requirements.txt
```

## Environment Variables

Copy the example and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```
DERIBIT_CLIENT_ID=your_client_id
DERIBIT_CLIENT_SECRET=your_client_secret
```

These are referenced from `config.yaml` via the `${VAR_NAME}` syntax and substituted at load time.

---

## Operating Modes

The system supports three modes, controlled by `system.mode` in `config.yaml`.

### 1. Live Mode

Connects to the Arbitrum chain and Deribit exchange. Places real orders with real money.

```yaml
system:
  mode: "live"
```

```bash
python main.py
```

What happens:
- Connects to Arbitrum RPC and subscribes to Swap events on all configured pools.
- Connects to Deribit, authenticates, subscribes to order book, order updates, trade fills, and portfolio.
- Signal engine processes every swap event and emits signals.
- Execution manager places real limit orders on Deribit.
- Risk manager runs a 1-second monitor loop checking stop-loss and take-profit conditions.
- Periodic heartbeat logs system state.
- Graceful shutdown on SIGINT/SIGTERM.

### 2. Paper Mode

Identical pipeline to live mode, but orders are logged instead of submitted to Deribit. Useful for validating signal quality without capital at risk.

```yaml
system:
  mode: "paper"
```

```bash
python main.py
```

What happens:
- Same as live mode: connects to both Arbitrum and Deribit (to receive real book data for mid-price calculations).
- When the execution manager would place an order, it instead logs a synthetic order ID and all order details.
- All signals, simulated orders, and position tracking are persisted to the database for later analysis.

### 3. Backtest Mode

Replays historical swap events stored in the SQLite database through the signal engine and simulates fills against 1 bps pool prices.

```yaml
system:
  mode: "backtest"
```

**Option A** — Run via `main.py` (backtests all configured pairs over the full dataset):

```bash
python main.py
```

**Option B** — Run the dedicated backtest entry point (customizable time range):

```bash
python backtest.py
```

Edit `backtest.py`'s `main()` function to set the pair name and timestamp range:

```python
result = await engine.run(
    pair_name="ETH-USDC",
    start_timestamp=1700000000,
    end_timestamp=1710000000,
)
```

What happens:
- Does **not** connect to Arbitrum or Deribit.
- Reads all swap events from the database in the specified time range.
- Feeds them through the signal engine to generate signals.
- Simulates order fills: for each entry signal, computes a limit price and scans forward through 1 bps pool events to see if the price crosses the limit within the staleness window.
- Exits are simulated as market fills at the 1 bps pool price with configurable block lag.
- Outputs: total signals, fill rate, win rate, average net return (bps), Sharpe ratio, max drawdown, total PnL, and an alpha decay curve.

**Populating data for backtesting:** Run the system in live or paper mode first to collect swap events into the database. Alternatively, import historical data directly into the `swap_events` table.

---

## Running Tests

```bash
python -m pytest tests/ -v
```

The test suite covers:
- **Signal Engine** (7 tests): conviction accumulation, hysteresis, momentum flag, autocorrelation filtering, transition detection, decay over time.
- **Execution Manager** (5 tests): order placement, exit logic, reversals, staleness cancellation, signal-proportional sizing.
- **Risk Manager** (5 tests): cooldown rejection, position limits, daily loss limits, stop-loss/take-profit triggers.
- **Backtest** (4 tests): empty data handling, trending data, alpha decay computation, fill rate bounds.

---

## Configuration Reference

All configuration lives in `config.yaml`. Each section is documented below.

### `system`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `log_level` | string | `"INFO"` | Python logging level. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` logs every swap event and signal recomputation. |
| `heartbeat_interval_seconds` | int | `60` | How often (in seconds) the system logs a status heartbeat showing current signal, conviction, and position for each pair. |
| `database_path` | string | `"data/events.db"` | Path to the SQLite database file. Created automatically if it doesn't exist. Parent directories are created as needed. |
| `mode` | string | `"live"` | Operating mode: `"live"` (real orders), `"paper"` (simulated orders), or `"backtest"` (replay historical data). |

### `infrastructure`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `rpc_url` | string | — | WebSocket RPC endpoint for Arbitrum. Must be a `wss://` URL. Used to subscribe to on-chain Swap events. |
| `chain_id` | int | `42161` | EVM chain ID. `42161` = Arbitrum One mainnet. |
| `block_confirmations` | int | `1` | Number of block confirmations before processing events. Higher values increase latency but reduce reorg risk. |
| `reconnect_delay_seconds` | int | `5` | Seconds to wait before attempting to reconnect after an RPC WebSocket disconnect. |
| `max_reconnect_attempts` | int | `50` | Maximum consecutive reconnection attempts before the system shuts down with an error. |

### `deribit`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | string | `"https://www.deribit.com"` | Deribit REST API base URL. Use `"https://test.deribit.com"` for testnet. |
| `ws_url` | string | `"wss://www.deribit.com/ws/api/v2"` | Deribit WebSocket endpoint. Use `"wss://test.deribit.com/ws/api/v2"` for testnet. |
| `client_id` | string | — | Deribit API client ID. Typically loaded from environment via `${DERIBIT_CLIENT_ID}`. |
| `client_secret` | string | — | Deribit API client secret. Typically loaded from environment via `${DERIBIT_CLIENT_SECRET}`. |

### `pairs[]`

Each entry in the `pairs` list configures one trading pair. The system processes all pairs concurrently.

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Human-readable pair name, e.g. `"ETH-USDC"`. Used as the key throughout the system. |
| `deribit_instrument` | string | Deribit perpetual contract name, e.g. `"ETH-PERPETUAL"`. |
| `token0` | string | Contract address of token0 in the Uniswap V3 pool on Arbitrum. |
| `token1` | string | Contract address of token1 in the Uniswap V3 pool on Arbitrum. |
| `token0_decimals` | int | Decimal places for token0 (e.g. 18 for WETH). Used to convert raw pool prices. |
| `token1_decimals` | int | Decimal places for token1 (e.g. 6 for USDC). Used to convert raw pool prices. |
| `invert_price` | bool | If `true`, the price is inverted (1/P) after decimal adjustment. Set to `true` when the pool's native price is base/quote but you want quote/base. |

### `pairs[].pools`

Three Uniswap V3 pool addresses per pair, one for each fee tier:

| Key | Description |
|-----|-------------|
| `bp30` | The 30 bps (0.30%) fee tier pool. Used by Layer 1 of the signal engine for trend detection. This is the lowest-liquidity, most informationally rich pool. |
| `bp5` | The 5 bps (0.05%) fee tier pool. Used by Layer 2 for momentum confirmation and autocorrelation measurement. |
| `bp1` | The 1 bps (0.01%) fee tier pool. Used as a reference price source. In backtesting, it proxies the Deribit mid price for simulated fills. |

Each pool has:
| Parameter | Type | Description |
|-----------|------|-------------|
| `address` | string | Uniswap V3 pool contract address on Arbitrum. |
| `fee` | int | Pool fee in hundredths of a basis point (3000 = 30 bps, 500 = 5 bps, 100 = 1 bps). |

### `pairs[].signal`

Controls the two-layer signal computation.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `conviction_halflife_seconds` | float | `480` | Half-life for the exponential decay applied to conviction before each update. At 480s (8 min), conviction from a 30 bps event has halved in influence after 8 minutes. This lets conviction accumulate across a cluster of swaps spanning several minutes. |
| `trend_entry_threshold` | float | `1.5` | Conviction must exceed this value for the trend state to transition to +1 or -1. Higher values require stronger evidence before entering a trend. |
| `trend_exit_threshold` | float | `0.5` | If absolute conviction drops below this value, the trend state resets to 0 (neutral). Must be strictly less than `trend_entry_threshold`. The gap between exit and entry creates a hysteresis band that prevents rapid flip-flopping. |
| `intensity_smoothing` | float | `0.85` | Exponential smoothing factor (beta) for the average inter-event time on the 30 bps pool. Values closer to 1.0 smooth more aggressively, dampening spikes. Used to compute event arrival intensity. |
| `momentum_window_events` | int | `30` | Number of recent 5 bps log-returns to keep in the rolling window. Cumulative momentum and lag-1 autocorrelation are computed over this window. A 30-event window captures several minutes of flow. |
| `min_autocorrelation` | float | `0.05` | Minimum lag-1 autocorrelation required for the momentum flag to activate. Ensures that price moves in the 5 bps pool exhibit serial correlation (trending behavior), not noise. |
| `conviction_cap` | float | `3.0` | Maximum absolute value of conviction. Clamps conviction to [-cap, +cap] after every update. Prevents runaway conviction from one-sided markets. Also acts as the denominator in the combined signal's magnitude scaling. |

### `pairs[].execution`

Controls order placement behavior.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `offset_base_bps` | float | `2.0` | Minimum price offset (in basis points) from the Deribit mid price when placing limit orders. This is the tightest the order will be placed, even at maximum conviction. Provides room for the market to come to you. |
| `offset_conviction_bps` | float | `6.0` | Additional offset (in bps) applied inversely proportional to signal conviction. At low conviction, the full 6 bps is added (total 8 bps offset). At maximum conviction, zero is added (total 2 bps offset). |
| `stale_order_seconds` | float | `45` | If an order remains unfilled after this many seconds, it is automatically cancelled. Gives limit orders adequate time to fill while the market oscillates. |
| `allow_reprice` | bool | `false` | Reserved for future use. When `true`, would allow the system to amend unfilled orders to a new price rather than cancel-and-replace. |
| `max_reprice_bps` | float | `3.0` | Maximum price movement (in bps) allowed for a single reprice. Only relevant if `allow_reprice` is `true`. |
| `post_only` | bool | `true` | When `true`, all limit orders are placed as post-only (maker only). If the order would immediately match, Deribit rejects it. This ensures the system always earns the maker rebate and avoids taker fees. |

### `pairs[].risk`

Controls position sizing limits and risk management.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_position_usd` | float | `50000` | Maximum notional value (in USD) of a single order. Orders where `size * price` exceeds this are rejected by the risk manager. |
| `max_position_contracts` | float | `100` | Maximum position size in contracts. Also serves as the base for signal-proportional sizing: `size = round(|S_t| * max_position_contracts)`. |
| `stop_loss_bps` | float | `35` | If unrealized loss on an open position exceeds this many basis points, the risk manager immediately places a market order to close the position. Wide enough to tolerate normal noise without being shaken out. |
| `take_profit_bps` | float | `50` | If unrealized profit exceeds this many basis points, the risk manager places an exit order. Lets winning trades run to capture the full momentum move. |
| `cooldown_seconds` | float | `120` | After a stop-loss exit, no new orders for this pair are allowed for this many seconds. Prevents re-entering during choppy conditions that just caused a loss. |
| `daily_loss_limit_usd` | float | `500` | Maximum cumulative net loss (in USD) allowed over the trailing 24 hours. If breached, all new orders for this pair are rejected until losses roll off. |
| `max_open_orders` | int | `3` | Maximum number of simultaneously open (unfilled) orders per pair. |
| `margin_usage_limit_pct` | float | `50` | Maximum percentage of account equity that can be consumed by margin. Checked via Deribit's account summary before each order. Must be between 1 and 100. |
| `max_holding_seconds` | float | `1800` | Hard cap on how long a position can be held (30 minutes). If a position has been open for this many seconds, the risk manager force-exits it at market regardless of PnL or signal state. Set to 0 to disable. Prevents holding through regime changes or funding rate periods. |

---

## Position Sizing Logic

The system uses **signal-proportional sizing** combined with a **risk manager veto chain**. Here is exactly how the trade size is determined each time:

### Step 1: Signal Magnitude

The signal engine produces a combined signal S_t in the range [-1, +1]:

```
S_t = T_30 * F_5 * min(|C_30| / C_max, 1)
```

Where:
- **T_30** is the trend state from the 30 bps pool: +1 (bullish), -1 (bearish), or 0 (neutral).
- **F_5** is the momentum flag from the 5 bps pool: 1 if momentum direction aligns with the trend AND lag-1 autocorrelation exceeds the minimum threshold; 0 otherwise.
- **C_30** is the current conviction (accumulated directional evidence, decayed over time).
- **C_max** is the conviction cap (default 3.0).

The **sign** of S_t determines the direction (LONG or SHORT). The **absolute value** determines how aggressively to size.

### Step 2: Raw Size Calculation

The raw position size is computed as:

```
size = round(|S_t| * max_position_contracts)
```

With a minimum of 1 contract. For example, with `max_position_contracts: 100`:

| Signal |S_t| | Computed Size |
|-----------|---------------|
| 0.3 | 30 contracts |
| 0.5 | 50 contracts |
| 0.8 | 80 contracts |
| 1.0 | 100 contracts |

This means that at low conviction the system takes a small position, and at high conviction it scales up to the maximum.

### Step 3: Limit Price Offset

The limit price is set as an offset from the current Deribit mid price, also scaled by conviction:

```
offset_bps = offset_base_bps + offset_conviction_bps * (1 - |S_t|)
offset_usd = mid_price * offset_bps / 10000
```

- For a **LONG** entry: `limit_price = mid_price - offset_usd`
- For a **SHORT** entry: `limit_price = mid_price + offset_usd`

At high conviction (`|S_t|` near 1.0), the offset shrinks toward `offset_base_bps` only (tight, aggressive). At low conviction (`|S_t|` near 0), the offset widens to `offset_base_bps + offset_conviction_bps` (passive, more spread).

The final price is rounded to the instrument's tick size (0.5 for ETH-PERPETUAL).

### Step 4: Risk Manager Approval

Before the order is placed, the risk manager checks (in order):

1. **Cooldown** — Is this pair in a post-stop-loss cooldown period?
2. **Daily Loss** — Has cumulative 24h net PnL exceeded the daily loss limit?
3. **Position Limit** — Would the new total position exceed `max_position_contracts`?
4. **Notional Limit** — Would `size * price` exceed `max_position_usd`?
5. **Margin Check** — Would total margin usage exceed `margin_usage_limit_pct` of equity?

If any check fails, the order is rejected and the reason is logged.

### Step 5: Scaling and Reducing

After the initial entry, if the signal magnitude changes while the direction stays the same:

- **Scale up** (signal increased): An additional limit order is placed for the difference between the current position and the new target size.
- **Reduce** (signal decreased): A reduce-only limit order is placed to trim the position down to the new target size.
- **Reversal** (direction flipped): The current position is closed at market, then a new entry is placed in the opposite direction.
- **Exit** (signal went to zero): All open orders are cancelled and the position is closed at market.

This means the position size is continuously adjusted to match the system's current conviction level.
