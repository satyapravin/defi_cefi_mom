# Cross-Venue Momentum Trading System — BP30 Swap Cluster Strategy

A Python 3.11+ asyncio system that monitors Uniswap V3 swap events across three fee-tier pools (30 bps, 5 bps, 1 bps) on Arbitrum, detects **directional clusters of 30 bps swaps** in real-time, and places maker limit orders on Deribit perpetual futures — trading only when a cluster of urgent, same-direction flow is detected in the expensive pool, with cross-tier confirmation, autocorrelation gating, and regime-aware position sizing.

## Strategy Overview

### The Core Insight

Uniswap V3's fee-tier structure creates a natural **self-selection mechanism**. When a trader chooses to swap on the 30 bps pool instead of the 5 bps pool, they voluntarily pay 6x the fee. The only rational explanation is urgency — they have time-sensitive information and prioritize execution speed over cost. This is the fingerprint of informed flow.

The system watches for **clusters of same-direction swaps** in the 30 bps pool. A single expensive swap could be noise, but 3+ swaps in the same direction within a 2-minute window, weighted by recency, is a strong signal that multiple informed actors are trading on the same view.

### How It Works

```
Uniswap V3 Pools (Arbitrum)          Signal Pipeline              Deribit (CEX)
┌─────────────────────────┐
│  30 bps Pool Swaps      │──► BP30 Signal Engine ──► Directional ──► Place limit
│  (cluster detection)    │    (exp-decay weight,     cluster         order on
│                         │     direction ratio)      detected?       Deribit
│  5 bps Pool Swaps       │──► Cross-Tier         ──► BP5 agrees?    perpetual
│  (coherence check)      │    Coherence
│                         │
│  5 bps Log Returns      │──► Regime Filter      ──► Not mean-
│  (vol + autocorrelation)│    (QUIET/ACTIVE/         reverting?
│                         │     CHAOTIC + ACF)
└─────────────────────────┘
```

1. **BP30 Swap Cluster Detection** — The system maintains a rolling 2-minute window of 30 bps swaps. Each swap is assigned an exponential-decay weight (`w = exp(-0.01 × age_seconds)`, half-life ~69s) so recent swaps matter more. When the window contains ≥3 swaps and ≥70% of the decay-weighted return mass points in one direction, a signal fires.

2. **BP5 Cross-Tier Coherence** — The 5 bps pool provides confirmation. Over the last 60 seconds of BP5 swaps, the system measures what fraction agree with the BP30 direction. If BP5 has ≥5 events, coherence modulates signal strength via `factor = 0.5 + 0.5 × coherence`. If BP5 has fewer than 5 events, coherence defaults to 1.0 (neutral).

3. **Regime Filter** — Realized volatility from BP5 log returns (5-minute window) classifies the market into three states:
   - **QUIET** (vol < 0.00004): Low activity, signals are rare but clean. Size at 1.0×.
   - **ACTIVE** (vol 0.00004–0.00006): Best regime — frequent, predictive signals. Size at 1.5×.
   - **CHAOTIC** (vol > 0.00006): Can't distinguish informed flow from panic. Size at 0.3×.

4. **Autocorrelation Gating** — Lag-1 autocorrelation of BP30 returns (50-event window) detects the microstructure regime:
   - ACF > 0.15 → trending → 1.3× multiplier (momentum confirmed)
   - ACF < −0.15 → mean-reverting → 0.3× multiplier (suppress — signal will reverse)
   - Otherwise → neutral → 1.0×

5. **Execution** — When all conditions align, the system places a post-only limit order on Deribit ETH-PERPETUAL with signal-adaptive sizing and offset pricing. Unfilled orders are cancelled after 45 seconds.

### Why It Can Work

- **Structural information advantage** — Fee-tier self-selection is embedded in the Uniswap V3 protocol. It can't be arbitraged away.
- **Cluster detection is robust** — Single expensive swaps are noise; directional clusters of 3+ are signal. Exponential decay ensures stale swaps don't pollute.
- **Cross-tier confirmation reduces false positives** — BP5 coherence catches cases where 30 bps activity is just random routing, not informed flow.
- **ACF gating avoids mean-reversion traps** — The single biggest failure mode for momentum is entering when the microstructure is mean-reverting. ACF catches this.
- **The regime filter protects capital** — Explicit regime detection prevents trading during volatility events where signal quality degrades.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        EventListener                             │
│  WebSocket to Arbitrum RPC (Alchemy)                            │
│  Subscribes to Swap events on all 3 pool contracts              │
└───────────┬──────────────────────────────────────────────────────┘
            │ SwapEvent
            ▼
┌───────────────────┐
│  RegimeFilter     │
│  (BP5 realized    │
│   vol, intensity, │
│   BP30 ACF,       │
│   QUIET/ACTIVE/   │
│   CHAOTIC)        │
└────────┬──────────┘
         │
         ▼
┌───────────────────────────────────┐
│  BP30SignalEngine                 │
│  (exp-decay cluster detection,   │
│   direction ratio threshold,     │
│   BP5 coherence confirmation)    │
└────────┬──────────────────────────┘
         │ TradeSignal
         ▼
┌───────────────────────────────────┐
│  ExecutionManager                 │
│  (signal-adaptive sizing,        │
│   conviction-based offset,       │
│   post-only limit orders,        │
│   stale order cancellation)      │
└────────┬──────────────────────────┘
         │
         ▼
┌───────────────────────────────────┐
│  RiskManager                     │
│  (regime-aware stops, SL/TP,     │
│   max holding time, cooldown,    │
│   daily loss limit, margin)      │
└───────────────────────────────────┘
```

### Key Files

| File | Purpose |
|------|---------|
| `signal_30bps.py` | Core signal engine — exp-decay-weighted directional cluster detection on 30 bps swaps, BP5 cross-tier coherence, ACF regime gating, signal emission |
| `regime_filter.py` | Rolling realized vol from BP5 returns, BP30 autocorrelation, event intensity, regime classification (QUIET/ACTIVE/CHAOTIC), ACF multiplier |
| `event_listener.py` | Arbitrum RPC WebSocket subscription for Swap events, log parsing, reconnection with backoff |
| `execution_manager.py` | Order lifecycle — signal-adaptive sizing, limit price computation (conviction offset), stale order cancellation |
| `risk_manager.py` | Pre-trade checks (cooldown, daily loss, position limits, margin), regime-aware stop-loss widening, take-profit, max holding time |
| `deribit_client.py` | Deribit WebSocket API client — authentication, order placement, portfolio subscription |
| `backtest.py` | Replay historical swaps through the BP30 signal engine, simulate fills against BP5 prices, compute metrics and alpha decay |
| `run_backtest.py` | Standalone backtest runner with parameter sweep mode |
| `parameter_sweep.py` | Sensitivity analysis and walk-forward validation across key parameters |
| `historical_loader.py` | Fetch historical Swap events from Arbitrum RPC by date/block range, populate the SQLite database |
| `data_quality.py` | Database health checker — event counts, time coverage, block gaps, price sanity |
| `analyze.py` | Visualization dashboard — generates 7 chart types (price timeline, event heatmap, volume, equity curve, alpha decay, PnL distribution, regime pie) |
| `database.py` | Async SQLite persistence for swap events, signals, orders, trades |
| `config.py` | Pydantic configuration models with validation |
| `models.py` | Data models — SwapEvent, TradeSignal, TradeRecord, Direction, Regime, FeeTier, etc. |
| `logger.py` | Structured logging setup |
| `main.py` | Entry point — wires all components, manages lifecycle |

---

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
- Swap events flow through RegimeFilter → BP30SignalEngine → ExecutionManager → RiskManager.
- When a directional cluster is detected in the 30 bps pool, BP5 coherence confirms, and regime/ACF allow — a TradeSignal is emitted.
- Execution manager places a post-only limit order on Deribit with signal-adaptive sizing.
- Risk manager runs a 1-second monitor loop with regime-aware stop-loss widening.
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

### 3. Backtest Mode

Replays historical swap events stored in the SQLite database through the full signal pipeline and simulates fills against BP5 pool prices.

**Option A** — Dedicated backtest runner (recommended):

```bash
# Single run with default parameters
python run_backtest.py

# Parameter sweep across direction_ratio, min_cluster_swaps, and window_seconds
python run_backtest.py --sweep
```

**Option B** — Via `backtest.py` directly:

```bash
python backtest.py
```

**Option C** — Via `main.py` with config set to backtest:

```yaml
system:
  mode: "backtest"
```

```bash
python main.py
```

What happens in backtest:
- Reads all swap events from the database for the specified time range.
- Feeds swaps through `RegimeFilter` + `BP30SignalEngine` to generate `TradeSignal`s.
- Simulates order fills: computes signal-adaptive size and limit price, scans forward through BP5 events to check if the price crosses within the staleness window (45s).
- Exits on stop-loss (40 bps), take-profit (40 bps), max holding time (600s), or end of data.
- Outputs: signal count, fill rate, win rate, Sharpe ratio, max drawdown, total PnL, alpha decay curve, and regime distribution.

**Populating data for backtesting:** Use the historical loader to fetch on-chain data:

```bash
# By date range (recommended)
python historical_loader.py \
  --rpc-url https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY \
  --start-date 2024-01-01 --end-date 2024-02-01

# By block range
python historical_loader.py \
  --rpc-url https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY \
  --start-block 170000000 --end-block 180000000

# Resume a partially completed load
python historical_loader.py \
  --rpc-url https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY \
  --start-date 2024-01-01 --end-date 2024-02-01 --resume
```

The loader fetches Swap events in batches, resolves block timestamps, and inserts into the SQLite database. It auto-reduces batch size if the RPC rejects large ranges and retries with exponential backoff. HTTP RPC URLs are recommended over WebSocket for large historical fetches.

---

## Running Tests

```bash
python -m pytest tests/ -v
```

The test suite covers 9 files:

- **BP30 Signal** (`test_signal_30bps.py`): Cluster detection, direction ratio threshold, coherence modulation, position state management.
- **Signal Strength** (`test_signal_strength.py`): Exp-decay weighting, coherence factor, ACF multiplier interaction.
- **Regime Filter** (`test_regime_filter.py`): QUIET/ACTIVE/CHAOTIC classification, intensity tracking, regime multiplier values, ACF computation, window expiry.
- **Execution Manager** (`test_execution_manager.py`): Order placement, exit logic, reversals, staleness cancellation, signal-proportional sizing.
- **Risk Manager** (`test_risk_manager.py`): Cooldown rejection, position limits, daily loss limits, stop-loss/take-profit triggers.
- **Backtest** (`test_backtest.py`): Empty data handling, trending data, alpha decay computation, fill rate bounds.
- **Parameter Sweep** (`test_parameter_sweep.py`): Config modification isolation, deep copy safety, best-Sharpe selection, plateau detection, sensitivity classification.
- **Historical Loader** (`test_historical_loader.py`): Byte/hex conversion, int24 decoding, price computation, swap log parsing.
- **Hypothesis Tests** (`test_hypothesis.py`): Statistical tests for signal validity.

---

## Parameter Tuning

### Quick Sweep via `run_backtest.py`

The simplest way to sweep parameters:

```bash
python run_backtest.py --sweep
```

This sweeps `direction_ratio` × `min_cluster_swaps` × `window_seconds` and reports the best combinations by PnL, trade count, and Sharpe ratio.

### Full Sensitivity Analysis via `parameter_sweep.py`

Sweeps each parameter independently and reports: Sharpe ratio, PnL, win rate, trade count, and max drawdown at each value. Identifies the plateau and classifies sensitivity as LOW, MEDIUM, or HIGH.

```bash
# Sweep all key parameters
python parameter_sweep.py --pair ETH-USDC --start 1700000000 --end 1710000000

# Sweep a single parameter
python parameter_sweep.py --param direction_ratio
```

### Walk-Forward Validation

Splits the time range into rolling folds. For each fold, trains on the first 70% and tests on the remaining 30%. Reports whether the parameter is **STABLE** or **UNSTABLE**.

```bash
python parameter_sweep.py --walk-forward --folds 5
```

### Interpreting Results

- **Wide plateau** = robust parameter. Pick the middle of the plateau.
- **Narrow peak** = fragile. Likely noise — leave at the default.
- **Sharpe decay ratio** (walk-forward) > 0.5 = acceptable. < 0.5 = overfitting.
- **Chosen values vary across folds** = the parameter doesn't generalize.

---

## Data Quality

Before running backtests, verify the loaded data:

```bash
# Default — uses config.yaml to find the DB path
python data_quality.py

# Explicit database path
python data_quality.py --db data/events.db --pair ETH-USDC

# Machine-readable JSON output
python data_quality.py --json
```

The report includes:
- **Event counts** by type (swap) and fee tier
- **Time coverage** — first/last event timestamps, total hours, events per hour
- **Price sanity** — average, min, max, zero-price count, extreme return count per tier
- **Block gap detection** — identifies 10+ minute gaps in BP5 data that may indicate missing blocks

---

## Analysis Dashboard

Generate charts from the database:

```bash
# Full dashboard (includes a backtest run for equity/PnL charts)
python analyze.py --pair ETH-USDC --hours 24

# Data-only mode (skip backtest — faster)
python analyze.py --data-only --hours 12

# Custom output directory
python analyze.py --out my_charts/
```

Charts generated:

| # | Chart | Description |
|---|-------|-------------|
| 1 | Price timeline | Swap prices over time, coloured by fee tier |
| 2 | Event rate heatmap | 5-minute bin event intensity by tier |
| 3 | Tier volume | Swap count and estimated USDC volume per tier |
| 4 | Equity curve | Cumulative PnL from backtest |
| 5 | Alpha decay | BP30 signal continuation in bps at increasing horizons (1, 2, 5, 10, 20, 50, 100 BP5 events) |
| 6 | Trade PnL distribution | Histogram of per-trade net PnL |
| 7 | Regime distribution | Pie chart of QUIET/ACTIVE/CHAOTIC time allocation |

All charts use a dark theme and are saved as 150 DPI PNGs in the output directory (default: `plots/`).

---

## Configuration Reference

All configuration lives in `config.yaml`. Each section is documented below.

### `system`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `log_level` | string | `"INFO"` | Python logging level. |
| `heartbeat_interval_seconds` | int | `60` | How often the system logs a status heartbeat. |
| `database_path` | string | `"data/events.db"` | Path to the SQLite database file. |
| `mode` | string | `"live"` | Operating mode: `"live"`, `"paper"`, or `"backtest"`. |

### `infrastructure`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `rpc_url` | string | — | WebSocket RPC endpoint for Arbitrum (`wss://` URL). |
| `chain_id` | int | `42161` | EVM chain ID. `42161` = Arbitrum One mainnet. |
| `block_confirmations` | int | `1` | Block confirmations before processing events. |
| `reconnect_delay_seconds` | int | `5` | Seconds to wait before reconnecting after disconnect. |
| `max_reconnect_attempts` | int | `50` | Maximum consecutive reconnection attempts. |

### `deribit`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | string | `"https://www.deribit.com"` | Deribit REST API base URL. Use `"https://test.deribit.com"` for testnet. |
| `ws_url` | string | `"wss://www.deribit.com/ws/api/v2"` | Deribit WebSocket endpoint. |
| `client_id` | string | — | API client ID, typically `${DERIBIT_CLIENT_ID}`. |
| `client_secret` | string | — | API client secret, typically `${DERIBIT_CLIENT_SECRET}`. |

### `pairs[]`

Each entry configures one trading pair. The system processes all pairs concurrently.

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Human-readable pair name, e.g. `"ETH-USDC"`. |
| `deribit_instrument` | string | Deribit perpetual contract, e.g. `"ETH-PERPETUAL"`. |
| `token0` / `token1` | string | Contract addresses on Arbitrum. |
| `token0_decimals` / `token1_decimals` | int | Decimal places (18 for WETH, 6 for USDC). |
| `invert_price` | bool | If `true`, price is inverted (1/P) after decimal adjustment. |

### `pairs[].pools`

Three Uniswap V3 pool addresses per pair:

| Key | Description |
|-----|-------------|
| `bp30` | 30 bps fee tier pool — primary signal source for cluster detection. |
| `bp5` | 5 bps fee tier pool — cross-tier coherence check and regime vol input. |
| `bp1` | 1 bps fee tier pool — price reference for backtest fills. |

### `pairs[].bp30_signal`

Controls the 30 bps swap cluster signal engine.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `window_seconds` | float | `120` | Rolling lookback window (2 min) for 30 bps swap cluster detection. |
| `min_cluster_swaps` | int | `3` | Minimum number of swaps in window to consider a cluster. |
| `direction_ratio` | float | `0.7` | Minimum fraction of decay-weighted return mass in one direction to fire a signal. Must be in (0.5, 1.0]. |
| `decay_alpha` | float | `0.01` | Exponential decay rate for swap weighting. Half-life = ln(2)/α ≈ 69 seconds. |
| `bp5_coherence_window_seconds` | float | `60` | Lookback window for BP5 cross-tier coherence measurement. |
| `bp5_coherence_min_events` | int | `5` | Minimum BP5 events needed in window for coherence to be active. Below this, coherence defaults to 1.0. |

### `pairs[].execution`

Controls order placement behavior.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `offset_base_bps` | float | `2.0` | Minimum price offset from mid price for limit orders. |
| `offset_conviction_bps` | float | `6.0` | Additional offset inversely proportional to signal strength. |
| `stale_order_seconds` | float | `45` | Cancel unfilled orders after this many seconds. |
| `allow_reprice` | bool | `false` | Reserved for future use. |
| `max_reprice_bps` | float | `3.0` | Maximum price movement for a reprice. |
| `post_only` | bool | `true` | Place all orders as post-only (maker only). |

### `pairs[].risk`

Controls risk management.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_position_usd` | float | `50000` | Maximum notional value per order. |
| `max_position_contracts` | float | `100` | Maximum position size in contracts. Base for signal-adaptive sizing. |
| `stop_loss_bps` | float | `40` | Stop-loss threshold. **Regime-aware**: widened 1.5× in CHAOTIC, tightened 0.8× in QUIET. |
| `take_profit_bps` | float | `40` | Take-profit threshold. Symmetric with stop-loss for 1:1 R:R. |
| `cooldown_seconds` | float | `120` | Post-stop-loss cooldown period (2 min). |
| `daily_loss_limit_usd` | float | `500` | Maximum 24h cumulative loss before halting. |
| `max_open_orders` | int | `3` | Maximum simultaneous unfilled orders per pair. |
| `margin_usage_limit_pct` | float | `50` | Maximum margin usage as percentage of equity. |
| `max_holding_seconds` | float | `600` | Hard cap on position hold time (10 min). Aligned with alpha peak from decay curve analysis. |

### `pairs[].regime`

Controls the volatility regime filter and autocorrelation gating.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `vol_window_seconds` | float | `300` | Rolling window (5 min) for realized volatility from BP5 log returns. |
| `vol_quiet_threshold` | float | `0.00004` | Realized vol below this = QUIET regime. |
| `vol_chaotic_threshold` | float | `0.00006` | Realized vol above this = CHAOTIC regime. Between the two = ACTIVE. |
| `intensity_window_seconds` | float | `60` | Window for measuring event arrival intensity (events/second). |
| `chaotic_multiplier` | float | `0.3` | Position size multiplier in CHAOTIC regime. |
| `active_multiplier` | float | `1.5` | Position size multiplier in ACTIVE regime. |
| `quiet_multiplier` | float | `1.0` | Position size multiplier in QUIET regime. |
| `acf_window_events` | int | `50` | Number of BP30 events for lag-1 autocorrelation computation. |
| `acf_trending_threshold` | float | `0.15` | ACF above this = trending regime (momentum confirmed). |
| `acf_mean_revert_threshold` | float | `-0.15` | ACF below this = mean-reverting regime (suppress signals). |
| `acf_trending_multiplier` | float | `1.3` | Size boost when ACF indicates trending. |
| `acf_mean_revert_multiplier` | float | `0.3` | Size reduction when ACF indicates mean-reversion. |

---

## Position Sizing

### Step 1: Signal Strength

The BP30 signal engine computes a raw directional strength from the decay-weighted swap returns:

```
raw_strength = |Σ(return_k × exp(-α × age_k))| / Σ(|return_k| × exp(-α × age_k))
```

This is then modulated by BP5 cross-tier coherence:

```
final_strength = raw_strength × (0.5 + 0.5 × coherence)
```

Where `coherence` is the fraction of recent BP5 swaps that agree with the BP30 direction (1.0 if insufficient BP5 data).

### Step 2: Regime and ACF Multipliers

Two independent multipliers are computed and combined:

| Regime | Condition | Multiplier |
|--------|-----------|------------|
| QUIET | vol < 0.00004 | 1.0× |
| ACTIVE | vol 0.00004–0.00006 | 1.5× |
| CHAOTIC | vol > 0.00006 | 0.3× |

| ACF State | Condition | Multiplier |
|-----------|-----------|------------|
| Trending | ACF > 0.15 | 1.3× |
| Neutral | −0.15 ≤ ACF ≤ 0.15 | 1.0× |
| Mean-reverting | ACF < −0.15 | 0.3× |

```
combined_multiplier = regime_multiplier × acf_multiplier
```

### Step 3: Position Size

```
size = round(signal_strength × combined_multiplier × max_position_contracts)
```

Example with `max_position_contracts = 100`:

| Signal Strength | Regime | ACF | Combined Mult | Size |
|-----------------|--------|-----|---------------|------|
| 0.85 | ACTIVE (1.5×) | Trending (1.3×) | 1.95× | 166 → capped at 100 |
| 0.90 | QUIET (1.0×) | Neutral (1.0×) | 1.0× | 90 contracts |
| 0.80 | ACTIVE (1.5×) | Neutral (1.0×) | 1.5× | 120 → capped at 100 |
| 0.95 | CHAOTIC (0.3×) | Mean-rev (0.3×) | 0.09× | 9 contracts |

### Step 4: Limit Price

```
offset_bps = offset_base_bps + offset_conviction_bps × (1 − signal_strength)
```

Higher signal strength = tighter offset (more aggressive). Lower strength = wider offset (more passive).

For a buy: `limit = mid − offset`. For a sell: `limit = mid + offset`.

### Step 5: Risk Checks

Before placing, the risk manager validates:

1. **Cooldown** — Is this pair in a post-stop-loss cooldown period (120s)?
2. **Daily Loss** — Has cumulative 24h net PnL exceeded $500?
3. **Position Limit** — Would total position exceed 100 contracts?
4. **Notional Limit** — Would `size × price` exceed $50,000?
5. **Margin Check** — Would margin usage exceed 50%?

### Step 6: Regime-Aware Stop-Loss

The stop-loss threshold adapts to the current regime:

| Regime | Stop-Loss Adjustment |
|--------|---------------------|
| QUIET | 0.8× (tighter — less noise to tolerate) |
| ACTIVE | 1.0× (default — 40 bps) |
| CHAOTIC | 1.5× (wider — avoid premature stop-outs in high vol) |

---

## Database Schema

The SQLite database stores:

| Table | Purpose |
|-------|---------|
| `swap_events` | Every Uniswap V3 Swap event across all three pools |
| `signal_log` | Signal engine state snapshots |
| `order_log` | All orders (placed, filled, cancelled) |
| `trade_log` | Completed round-trip trades with PnL |
