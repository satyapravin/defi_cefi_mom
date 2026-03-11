# Cross-Venue Momentum Trading System — Toxic Flow Oracle

A Python 3.11+ asyncio system that monitors Uniswap V3 events (Swaps, Mints, Burns) across three fee-tier pools (30 bps, 5 bps, 1 bps) on Arbitrum, classifies informed vs. uninformed flow in real-time using a **Flow Toxicity Index**, and places maker limit orders on Deribit perpetual futures — trading only when genuinely toxic flow is detected, with toxicity-adaptive position sizing and regime-aware risk management.

## Strategy Overview

> **New to this project?** Read [strategy.md](strategy.md) first — it explains the economic intuition, signals, and why this strategy works using plain English and real-world analogies. The section below is the condensed technical version.

### The Core Insight

Uniswap V3's fee-tier structure creates a natural **self-selection mechanism** that has no analog in traditional finance. When a trader chooses to swap on the 30 bps pool instead of the 5 bps pool, they are *voluntarily paying 6x the fee*. The only rational explanation is urgency — they have time-sensitive information and prioritize execution speed over cost. This is the fingerprint of informed flow.

The system exploits this by building a real-time classifier that separates informed from uninformed flow across all three fee tiers, and only trades on Deribit when the classifier fires with high confidence.

### How It Works (5-Minute Version)

```
Uniswap V3 Pools (Arbitrum)          Analysis Pipeline           Deribit (CEX)
┌─────────────────────┐
│  30 bps Pool Swaps  │──┐
│   5 bps Pool Swaps  │──┼──► Flow Toxicity    ──► Is FTI in     ──► Place limit
│   1 bps Pool Swaps  │──┤    Index (FTI)          top 20%?          order on
│                     │  │                                            Deribit
│  Mint/Burn Events   │──┼──► LP Behavior      ──► Do LPs agree?     perpetual
│  (all 3 pools)      │  │    Monitor
│                     │  │
│  1 bps Log Returns  │──┴──► Regime Filter    ──► Not CHAOTIC?
└─────────────────────┘
```

1. **Flow Toxicity Index (FTI)** — Every 30 seconds, the system aggregates swap flow across all three fee tiers and computes a toxicity score that combines five orthogonal dimensions:
   - **Tier-weighted directional flow** — The 30 bps pool is weighted 6x because paying 30 bps to trade is a strong information signal.
   - **Cross-tier coherence** — Is only the expensive pool active? That's the most toxic (2x boost). All tiers agreeing is normal arb (1x). Tiers disagreeing is noise (0x).
   - **Price impact asymmetry** — Is someone moving the 30 bps pool price disproportionately per dollar vs. the 5 bps pool?
   - **OFI concentration** — Per-tier Order Flow Imbalance: `(up - down) / (up + down)`. When the 30 bps pool has OFI of 0.9 but the 1 bps pool has 0.1, informed flow is concentrated in the expensive tier.
   - **Hawkes-inspired clustering** — Measures burst arrival of 30 bps events (fraction within 5 seconds of each other) and cross-excitation asymmetry (does 30 bps activity predict 5 bps activity, indicating information cascading from the expensive pool outward?).

2. **Regime Filter** — Realized volatility and event intensity from the 1 bps pool (highest frequency) classify the market into three states: QUIET (trade normally), ACTIVE (trade aggressively — best regime for signal quality), and CHAOTIC (sit out entirely — can't distinguish informed flow from panic). This is the key difference between a strategy that works in backtest and one that survives live.

3. **LP Behavior Monitor** — Sophisticated LPs on Uniswap V3 reposition *before* price moves. When LPs pull their asks (remove sell-side liquidity), they expect price to rise. This is on-chain "market maker tell" data. The system uses LP bias as a confirmation filter — only enter when LP behavior agrees with the FTI direction.

4. **Signal Gating** — A trade is only placed when *all three conditions* are met: FTI is in the top 20% of the trailing 24-hour distribution, the regime is not CHAOTIC, and LP bias agrees. Most of the time, the system does nothing.

5. **Toxicity-Adaptive Sizing** — Position size scales with the FTI percentile and regime: `size = FTI_percentile × regime_multiplier × max_contracts`. In the ACTIVE regime the multiplier is 1.5x; in QUIET it's 1.0x. This concentrates capital on the highest-conviction signals.

### Why It Can Work

- **Structural information advantage** — The fee-tier self-selection exists in the Uniswap V3 protocol design. It can't be arbitraged away.
- **Cross-tier analysis is underexplored** — Most quant firms monitoring DEX data watch only the highest-volume pool. Comparing flow across all three tiers is a differentiated signal.
- **The regime filter protects capital** — The #1 killer of cross-venue strategies is volatility events. Explicit regime detection addresses this.
- **LP behavior is genuinely leading** — Sophisticated on-chain market makers reposition before price moves, creating data that has no equivalent in traditional markets.
- **Aggressive filtering** — Trading only the top 20% of signals sacrifices frequency for quality. At 5–30 minute holding periods, expect 5–15 trades per day with higher win rates.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        EventListener                             │
│  Subscribes to Swap + Mint/Burn events on all 3 pool contracts  │
└───────────┬───────────────────────────────────┬──────────────────┘
            │ SwapEvent                         │ LiquidityEvent
            ▼                                   ▼
┌───────────────────┐  ┌──────────────┐  ┌──────────────┐
│  RegimeFilter     │  │ FlowToxicity │  │  LPMonitor   │
│  (realized vol,   │  │   Engine     │  │  (Mint/Burn  │
│   intensity,      │◄─┤  (FTI per    │─►│   bias)      │
│   QUIET/ACTIVE/   │  │   30s bucket)│  └──────────────┘
│   CHAOTIC)        │  └──────┬───────┘
└───────────────────┘         │ ToxicitySignal
                              ▼
                  ┌───────────────────────┐
                  │  ExecutionManager     │
                  │  (toxicity-adaptive   │
                  │   sizing, limit       │
                  │   order placement)    │
                  └───────────┬───────────┘
                              │
                  ┌───────────▼───────────┐
                  │  RiskManager          │
                  │  (regime-aware stops, │
                  │   margin, cooldown)   │
                  └───────────────────────┘
```

### Key Files

| File | Purpose |
|------|---------|
| `flow_toxicity.py` | Core intelligence — FTI computation per time bucket, cross-tier coherence, impact asymmetry, OFI concentration, Hawkes clustering, percentile gating, signal emission |
| `regime_filter.py` | Rolling realized vol from 1 bps returns, event intensity, regime classification (QUIET/ACTIVE/CHAOTIC) |
| `lp_monitor.py` | Mint/Burn event processing, LP bias calculation (net liquidity above vs. below current tick) |
| `event_listener.py` | Arbitrum RPC subscription for Swap + Mint/Burn events, log parsing, reconnection |
| `execution_manager.py` | Order lifecycle — toxicity-adaptive sizing, limit price computation, stale order cancellation |
| `risk_manager.py` | Pre-trade checks, regime-aware stop-loss widening, take-profit, max holding time enforcement |
| `signal_engine.py` | Legacy momentum signal engine (conviction + momentum). Still available for comparison backtests |
| `backtest.py` | Replay historical data through either the toxicity engine or the legacy signal engine |
| `database.py` | Async SQLite persistence for swap events, liquidity events, flow buckets, signals, orders, trades |
| `config.py` | Pydantic configuration models with validation |
| `models.py` | Data models — SwapEvent, LiquidityEvent, FlowBucket, ToxicitySignal, Regime, etc. |
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
- Connects to Arbitrum RPC and subscribes to Swap + Mint/Burn events on all configured pools.
- Connects to Deribit, authenticates, subscribes to order book, order updates, trade fills, and portfolio.
- Swap events flow through the RegimeFilter, LPMonitor, and FlowToxicityEngine pipeline.
- When FTI exceeds the percentile threshold, regime is not CHAOTIC, and LP bias agrees — a ToxicitySignal is emitted.
- Execution manager places real limit orders on Deribit with toxicity-adaptive sizing.
- Risk manager runs a 1-second monitor loop with regime-aware stop-loss widening.
- Periodic heartbeat logs regime, volatility, intensity, and position state.
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

Replays historical swap and liquidity events stored in the SQLite database through the full toxicity pipeline and simulates fills against 1 bps pool prices.

```yaml
system:
  mode: "backtest"
```

**Option A** — Run via `main.py` (runs the toxicity backtest for all configured pairs):

```bash
python main.py
```

**Option B** — Run the dedicated backtest entry point with a custom time range:

```bash
python backtest.py
```

Edit `backtest.py`'s `main()` function. Use `run_toxicity()` for the FTI-based strategy, or `run()` for the legacy momentum strategy:

```python
# Toxicity backtest
result = await engine.run_toxicity(
    pair_name="ETH-USDC",
    start_timestamp=1700000000,
    end_timestamp=1710000000,
)

# Legacy momentum backtest (for comparison)
result = await engine.run(
    pair_name="ETH-USDC",
    start_timestamp=1700000000,
    end_timestamp=1710000000,
)
```

What happens in the toxicity backtest:
- Reads all swap events and liquidity events from the database in the specified time range.
- Feeds swaps through `RegimeFilter` + `LPMonitor` + `FlowToxicityEngine` to generate `ToxicitySignal`s.
- Simulates order fills: computes toxicity-adaptive size and limit price, scans forward through 1 bps events to check if the price crosses within the staleness window.
- Exits on toxicity exit signal, max holding time, or end of data.
- Outputs: signals, fill rate, win rate, Sharpe ratio, max drawdown, total PnL, alpha decay curve, toxicity entry/exit counts, and regime distribution.

**Populating data for backtesting:** Run the system in live or paper mode first to collect events into the database. Alternatively, import historical data directly into the `swap_events` and `liquidity_events` tables.

---

## Running Tests

```bash
python -m pytest tests/ -v
```

The test suite covers 54 tests across 7 files:

- **Flow Toxicity** (18 tests): directional FTI, coherence boosts (isolated 30bps vs. aligned), incoherent tier suppression, exit on low percentile, CHAOTIC regime blocking, position close state reset, OFI computation (5 edge cases), OFI concentration (high/capped), cluster ratio (all/none/partial/single), cross-excitation (forward/reverse/symmetric/empty), clustered flow amplification.
- **Regime Filter** (7 tests): QUIET/ACTIVE/CHAOTIC classification, intensity tracking, regime multiplier values, window expiry, unknown pair defaults.
- **LP Monitor** (8 tests): mint/burn above/below tick bias direction, straddling range 50/50 split, window expiry, edge cases.
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
| `log_level` | string | `"INFO"` | Python logging level. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `heartbeat_interval_seconds` | int | `60` | How often the system logs a status heartbeat showing regime, vol, intensity, and position for each pair. |
| `database_path` | string | `"data/events.db"` | Path to the SQLite database file. Created automatically. |
| `mode` | string | `"live"` | Operating mode: `"live"`, `"paper"`, or `"backtest"`. |

### `infrastructure`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `rpc_url` | string | — | WebSocket RPC endpoint for Arbitrum (`wss://` URL). |
| `chain_id` | int | `42161` | EVM chain ID. `42161` = Arbitrum One mainnet. |
| `block_confirmations` | int | `1` | Block confirmations before processing events. |
| `reconnect_delay_seconds` | int | `5` | Seconds to wait before reconnecting after a disconnect. |
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
| `token0_decimals` / `token1_decimals` | int | Decimal places for each token (18 for WETH, 6 for USDC). |
| `invert_price` | bool | If `true`, price is inverted (1/P) after decimal adjustment. |

### `pairs[].pools`

Three Uniswap V3 pool addresses per pair:

| Key | Description |
|-----|-------------|
| `bp30` | 30 bps fee tier pool — lowest liquidity, most informationally rich. Primary input to FTI. |
| `bp5` | 5 bps fee tier pool — moderate liquidity. Cross-tier coherence input. |
| `bp1` | 1 bps fee tier pool — highest frequency. Drives regime filter (realized vol). Price reference for backtesting. |

### `pairs[].toxicity`

Controls the Flow Toxicity Index computation.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `bucket_seconds` | float | `30` | Duration (seconds) of each flow aggregation bucket. At 30s, the system computes one FTI value every 30 seconds. Shorter = more responsive but noisier. |
| `tier_weight_30` | float | `6.0` | Weight for 30 bps pool flow in the FTI. The 6x default reflects that paying 30 bps is a 6x stronger information signal than 1 bps. |
| `tier_weight_5` | float | `2.0` | Weight for 5 bps pool flow in the FTI. |
| `tier_weight_1` | float | `1.0` | Weight for 1 bps pool flow in the FTI. |
| `fti_percentile_threshold` | float | `0.80` | Only trade when the current FTI ranks in the top 20% of the trailing distribution. Higher = fewer but higher-quality trades. |
| `coherence_isolated_boost` | float | `2.0` | FTI multiplier when only the 30 bps pool has flow (no 5 bps or 1 bps activity). This is the strongest informed-flow signal: someone is paying the highest fee and there's no arb activity following them. |
| `coherence_aligned_boost` | float | `1.0` | FTI multiplier when all active tiers agree on direction. |
| `trailing_hours` | int | `24` | How many hours of FTI history to maintain for percentile computation. |
| `exit_fti_percentile` | float | `0.30` | Exit an existing position when FTI drops below the 30th percentile of the trailing distribution. |
| `cluster_halflife_seconds` | float | `15.0` | Exponential decay half-life for Hawkes-inspired intensity computation. |
| `cluster_window_seconds` | float | `5.0` | Two 30 bps events within this many seconds count as "clustered". High cluster ratio = informed order splitting. |
| `cross_excitation_window_seconds` | float | `10.0` | Time window to detect 30bps→5bps event cascades (information flowing from expensive to cheap pool). |
| `ofi_concentration_cap` | float | `5.0` | Maximum value for the OFI concentration ratio (`\|OFI_30\| / \|OFI_1\|`). Prevents extreme values in thin markets. |

### `pairs[].regime`

Controls the volatility regime filter.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `vol_window_seconds` | float | `300` | Rolling window (5 min) over which realized volatility is computed from 1 bps pool log-returns. |
| `vol_quiet_threshold` | float | `0.0005` | Realized vol below this = QUIET regime. |
| `vol_chaotic_threshold` | float | `0.003` | Realized vol above this = CHAOTIC regime. Between the two thresholds = ACTIVE. |
| `intensity_window_seconds` | float | `60` | Window for measuring event arrival intensity (events/second). |
| `chaotic_multiplier` | float | `0.3` | Position size multiplier in CHAOTIC regime (massively reduce exposure). |
| `active_multiplier` | float | `1.5` | Position size multiplier in ACTIVE regime (best conditions for the strategy). |
| `quiet_multiplier` | float | `1.0` | Position size multiplier in QUIET regime (normal sizing). |

### `pairs[].signal` (legacy)

Controls the original two-layer momentum signal. Still used by the legacy `SignalEngine` and `backtest.run()`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `conviction_halflife_seconds` | float | `480` | Half-life for exponential decay of conviction. |
| `trend_entry_threshold` | float | `1.5` | Conviction threshold to enter a trend. |
| `trend_exit_threshold` | float | `0.5` | Conviction threshold to exit a trend. |
| `intensity_smoothing` | float | `0.85` | Smoothing factor for inter-event time. |
| `momentum_window_events` | int | `30` | Rolling window size for 5 bps momentum. |
| `min_autocorrelation` | float | `0.05` | Minimum lag-1 autocorrelation for momentum flag. |
| `conviction_cap` | float | `3.0` | Max absolute conviction value. |

### `pairs[].execution`

Controls order placement behavior.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `offset_base_bps` | float | `2.0` | Minimum price offset from mid price when placing limit orders. |
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
| `max_position_contracts` | float | `100` | Maximum position size. Base for toxicity-adaptive sizing. |
| `stop_loss_bps` | float | `35` | Stop-loss threshold. **Regime-aware**: widened 1.5x in CHAOTIC, tightened 0.8x in QUIET. |
| `take_profit_bps` | float | `50` | Take-profit threshold. |
| `cooldown_seconds` | float | `120` | Post-stop-loss cooldown. |
| `daily_loss_limit_usd` | float | `500` | Maximum 24h cumulative loss. |
| `max_open_orders` | int | `3` | Maximum simultaneous unfilled orders per pair. |
| `margin_usage_limit_pct` | float | `50` | Maximum margin usage as percentage of equity. |
| `max_holding_seconds` | float | `1800` | Hard cap on position hold time (30 min). Set to 0 to disable. |

---

## Position Sizing: Toxicity-Adaptive

### Step 1: FTI Computation

Each 30-second bucket produces an FTI value using the full five-factor formula:

```
weighted_flow = 6.0 × flow_30 + 2.0 × flow_5 + 1.0 × flow_1

FTI = sign(weighted_flow)
    × log(1 + |weighted_flow|)
    × coherence              (0 or 0.5 or 1.0 or 2.0)
    × impact_asymmetry       (price_impact_30 / price_impact_5, capped at 5.0)
    × ofi_concentration      (|OFI_30| / |OFI_1|, capped at 5.0)
    × (1 + cluster_ratio)    (fraction of clustered 30bps events)
```

The OFI for each tier is `(up_count - down_count) / (up_count + down_count)`, measuring directional breadth independent of dollar volume. OFI concentration amplifies FTI when informed flow is concentrated in the expensive pool (high OFI_30, low OFI_1).

The cluster ratio measures burst arrival: what fraction of consecutive 30 bps events occur within 5 seconds of each other. This is a Hawkes self-excitation proxy — informed traders split large orders into rapid sequences. Cross-excitation asymmetry (30bps→5bps vs. 5bps→30bps event following) is recorded in the flow bucket for analysis but not directly in the FTI formula (it serves as a monitoring/diagnostic signal for strategy tuning).

The absolute FTI is then ranked against the trailing 24 hours of FTI values to produce a percentile (0 to 1). Only buckets where the percentile exceeds `fti_percentile_threshold` (default 0.80 = top 20%) are considered for trading.

### Step 2: Regime Multiplier

The regime filter classifies the current market state and applies a multiplier:

| Regime | Realized Vol | Multiplier | Interpretation |
|--------|-------------|------------|----------------|
| QUIET | < 0.0005 | 1.0x | Low activity, signals are rare but clean |
| ACTIVE | 0.0005 – 0.003 | 1.5x | Best regime — frequent, predictive signals |
| CHAOTIC | > 0.003 | 0.3x (or skip) | Can't distinguish informed flow from panic |

### Step 3: Position Size

```
size = round(FTI_percentile × regime_multiplier × max_position_contracts)
```

Example with `max_position_contracts = 100`:

| FTI Percentile | Regime | Multiplier | Size |
|----------------|--------|------------|------|
| 0.85 | ACTIVE | 1.5 | 128 → capped at 100 |
| 0.90 | QUIET | 1.0 | 90 contracts |
| 0.82 | ACTIVE | 1.5 | 123 → capped at 100 |
| 0.95 | CHAOTIC | 0.3 | 29 contracts |

### Step 4: Limit Price

Same offset logic as the legacy strategy, but using FTI percentile as the conviction proxy:

```
offset_bps = offset_base_bps + offset_conviction_bps × (1 - FTI_percentile)
```

Higher toxicity percentile = tighter offset (more aggressive). Lower percentile = wider offset (more passive).

### Step 5: Risk Manager Approval

Before placing, the risk manager checks:

1. **Cooldown** — Is this pair in a post-stop-loss cooldown period?
2. **Daily Loss** — Has cumulative 24h net PnL exceeded the limit?
3. **Position Limit** — Would total position exceed `max_position_contracts`?
4. **Notional Limit** — Would `size × price` exceed `max_position_usd`?
5. **Margin Check** — Would margin usage exceed the limit?

### Step 6: Regime-Aware Stop-Loss

The stop-loss threshold adapts to the current regime:

| Regime | Stop-Loss Adjustment |
|--------|---------------------|
| QUIET | 0.8× (tighter — less noise to tolerate) |
| ACTIVE | 1.0× (default) |
| CHAOTIC | 1.5× (wider — avoid premature stop-outs in high vol) |

---

## Database Schema

The SQLite database stores six tables:

| Table | Purpose |
|-------|---------|
| `swap_events` | Every Uniswap V3 Swap event across all three pools |
| `liquidity_events` | Every Mint/Burn event (LP additions/removals) |
| `flow_buckets` | Aggregated per-bucket flow data with per-tier breakdowns, OFI values, cluster ratio, and cross-excitation |
| `signal_log` | Legacy signal engine state snapshots |
| `order_log` | All orders (placed, filled, cancelled) |
| `trade_log` | Completed round-trip trades with PnL |
