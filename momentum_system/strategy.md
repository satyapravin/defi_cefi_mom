# Strategy: The Toxic Flow Oracle

## For Whom Is This Document?

Anyone who wants to understand *what* this system does, *why* it works, and *why it's hard to replicate* — without needing to read the code. If you're comfortable with the idea of "buy low, sell high" and have heard the word "blockchain," you have enough background.

For the technical implementation details (config parameters, database schema, how to run it), see [README.md](README.md).

---

## Part 1: The Setup — Two Worlds, One Price

There are two places where people trade Ethereum:

1. **Decentralized exchanges (DEXes)** like Uniswap, which live on a blockchain. Anyone can trade, everything is public, and there are no intermediaries.
2. **Centralized exchanges (CEXes)** like Deribit, which work like traditional brokerages. They have order books, margin trading, and professional market makers.

These two worlds trade the same asset (ETH), but information doesn't flow between them instantly. When a well-informed trader makes a big move on Uniswap, it takes seconds to minutes before the Deribit price catches up.

**This system watches Uniswap to detect informed traders in real-time, and then trades on Deribit before the price adjusts.**

Think of it like standing at a wholesale fish market at 4 AM. You notice a renowned sushi chef buying massive quantities of tuna. You know retail fish prices at the supermarket haven't updated yet. So you rush to the supermarket and buy their tuna before they raise the price.

---

## Part 2: Uniswap's Accidental Lie Detector

Here's what makes this strategy possible — and it's not something Uniswap designed on purpose.

### The Three Toll Roads

Uniswap V3 lets you trade ETH/USDC through three different pools, each with a different fee:

| Pool | Fee You Pay per Trade | Who Typically Uses It |
|------|----------------------|----------------------|
| **30 bps pool** | 0.30% ($300 per $100k trade) | Almost nobody... unless they're in a hurry |
| **5 bps pool** | 0.05% ($50 per $100k trade) | Regular traders, arbitrageurs |
| **1 bps pool** | 0.01% ($10 per $100k trade) | High-frequency bots, large passive trades |

Think of these as three toll roads connecting the same two cities:

- The **1 bps road** is the freeway — cheap, fast, everyone uses it.
- The **5 bps road** is a local highway — slightly more expensive, but fine.
- The **30 bps road** is a back alley with a $300 toll — nobody takes it unless they have a very good reason.

### Why Would Anyone Pay 30x More?

This is the key question, and the answer is the entire basis of the strategy.

If you're a regular trader, you'd always pick the cheapest pool. But if you know something — maybe you've spotted a vulnerability in a lending protocol, or you have advance knowledge of a large trade about to happen — then you need to act *now*. The cheap pools might not have enough liquidity to fill your order quickly, or you might move the price too much if you trade there. The expensive pool, despite its high fee, lets you get in and out without anyone noticing — because almost nobody watches it.

**Paying 30 bps when you could pay 1 bps is like paying $300 for a toll road when the freeway is free. The only reason to do it is if you know something that will make you far more than $300.**

This is what market microstructure researchers call "adverse selection" or "toxic flow." The 30 bps pool acts like an accidental lie detector: anyone who uses it is *probably* informed, because no rational uninformed trader would voluntarily pay 30x more in fees.

---

## Part 3: The Five Signals — Reading the Lie Detector

The system doesn't just look at whether someone traded on the 30 bps pool. It combines five independent measurements to build a confidence score called the **Flow Toxicity Index (FTI)**.

### Signal 1: Dollar-Weighted Directional Flow

The most basic measurement: which direction is money flowing, and how much?

If $500,000 flows into ETH (buying) on the 30 bps pool, that's weighted 6x more than the same flow on the 1 bps pool. Why 6x? Because someone paying 6x the fee is providing 6x the information signal.

**Analogy:** If a restaurant critic walks into an expensive restaurant and orders the tasting menu (expensive signal), that tells you more about the food quality than a tourist ordering the cheapest item (cheap signal).

### Signal 2: Cross-Tier Coherence — Agreement Between the Toll Roads

The system checks whether the three pools agree on direction:

- **Only the 30 bps pool has activity, and it's all buying** → This is the *strongest* signal (2x boost). An informed trader is quietly accumulating in the expensive, unwatched pool while the cheap pools show nothing. Classic stealth.
- **All three pools are buying** → Good signal (1x). The information is spreading across venues, which means it's probably real, but you're later to the party.
- **30 bps is buying, but 1 bps is selling** → Noise (0x signal). The pools disagree, so there's no clear informed direction.

**Analogy:** If the sushi chef is the *only* buyer at the fish market, that's a stronger signal than if everyone is buying. And if the chef is buying tuna but a seafood wholesaler is selling it, maybe neither of them knows something.

### Signal 3: Price Impact Asymmetry — Who's Moving the Market More?

For each dollar traded, how much does the price move?

If someone trades $100,000 on the 30 bps pool and moves the price by 0.5%, but $100,000 on the 5 bps pool only moves it by 0.1%, that's a 5:1 impact ratio. The 30 bps trader is absorbing more liquidity per dollar — they're eating through the order book. This is what urgency looks like.

**Analogy:** If someone walks into a store and buys every single unit of a product at full price (rather than waiting for a sale or negotiating), they probably need it badly.

### Signal 4: Order Flow Imbalance (OFI) Concentration

Instead of looking at dollar amounts, this signal counts *events*:

```
OFI = (number of buy swaps - number of sell swaps) / total swaps
```

This is computed separately for each pool. Then we compare:

```
OFI Concentration = |OFI of 30 bps pool| / |OFI of 1 bps pool|
```

When the 30 bps pool has 9 buys and 1 sell (OFI = 0.8), but the 1 bps pool has 50 buys and 45 sells (OFI = 0.05), the concentration ratio is 16:1. The expensive pool is overwhelmingly one-sided while the cheap pool is noisy — confirming that informed flow is isolated in the expensive tier.

Why is this different from Signal 1? Because Signal 1 measures dollar flow — one whale trade can dominate. OFI measures *breadth*: how many independent traders are all going the same direction. When both the dollar amount and the count of traders agree, the signal is much more robust.

**Analogy:** If one person buys $1 million of stock, that's notable. If 50 different people each independently buy $20,000, that's a *movement*. OFI captures the "many people agree" dimension.

### Signal 5: Hawkes Clustering — The Burst Pattern

Informed traders rarely place one big trade. They split large orders into many smaller ones to avoid detection — a practice called "order splitting" or "iceberg ordering." This creates a distinctive pattern: rapid bursts of trades on the same pool, with short gaps between them.

The system measures this by checking what fraction of consecutive 30 bps events occur within 5 seconds of each other (the "cluster ratio"):

- **Cluster ratio = 0.0**: Trades are spaced far apart. Probably random retail flow.
- **Cluster ratio = 0.8**: 80% of trades happen in rapid succession. Someone is executing a large order in pieces.

The system also checks "cross-excitation": after a burst on the 30 bps pool, does activity suddenly appear on the 5 bps pool? This pattern — expensive pool first, then cheaper pools follow — is the signature of information cascading through the market. It's like watching a rumor spread: the people who hear it first (30 bps) act immediately, and then others (5 bps, 1 bps) follow.

**Analogy:** If a store gets 20 customers in 2 minutes, all buying the same thing, that's not a coincidence. Compare that to 20 customers spread over 4 hours — that's just normal traffic. The burst pattern reveals coordinated or informed activity.

---

## Part 4: When NOT to Trade — The Regime Filter

Having a great signal isn't enough. You also need to know *when the signal is reliable*.

The system classifies the market into three states using volatility and trading activity from the cheapest (1 bps) pool:

| Regime | What's Happening | What We Do |
|--------|-----------------|------------|
| **QUIET** | Low volatility, sparse trading. The market is calm. | Trade with normal size. Signals are rare but tend to be high quality. |
| **ACTIVE** | Moderate volatility, regular flow. Things are moving, but not chaotically. | Trade with 1.5x normal size. This is the sweet spot — enough activity to generate signals, but not so much that everyone is panicking. |
| **CHAOTIC** | High volatility, intense activity. Something has happened (hack, liquidation cascade, macro news). | Don't trade at all. During chaos, *everyone* looks like an informed trader because everyone is rushing to trade. The lie detector stops working when the entire market is panicking. |

**Analogy:** A metal detector works great at the beach on a quiet day. It works even better when there's moderate foot traffic (more chances to find something). But during a thunderstorm when everyone is running around with umbrellas, it beeps constantly and finds nothing useful. The regime filter knows when to turn the metal detector off.

---

## Part 5: The Confirmation — LP Behavior Monitor

Uniswap V3 has a unique feature: liquidity providers (LPs) can choose *exactly* where in the price range to place their liquidity. Sophisticated LPs reposition their liquidity *before* expected price moves.

If LPs start pulling their sell-side liquidity (removing their offers to sell ETH), they're telling you: "I think ETH is about to go up, and I don't want to sell at this price." This is like watching the dealers at a car lot suddenly removing price tags from their best cars — they expect demand to spike.

The system tracks this LP repositioning and uses it as a final confirmation gate. We only trade when the LP behavior agrees with the FTI direction.

**Why this matters:** LPs are among the most sophisticated participants in DeFi. Many are run by quantitative firms. When their positioning agrees with the toxic flow signal, it's two independent sources of informed intelligence pointing the same way.

---

## Part 6: Putting It All Together — A Trade From Start to Finish

Here's what a typical trade looks like, end to end:

**13:42:00** — A 30-second window closes. During those 30 seconds:
- 8 buy swaps hit the 30 bps pool, totaling $200,000. No sells. (OFI = 1.0)
- 3 buy swaps and 2 sells on the 5 bps pool. (OFI = 0.2)
- 12 buys and 11 sells on the 1 bps pool. (OFI = 0.04)
- 6 of the 8 buys on the 30 bps pool happened within 3 seconds of each other. (Cluster ratio = 0.86)
- The 5 bps pool activity started 4 seconds after the 30 bps burst. (Cross-excitation: positive)

**13:42:00** — The system computes the FTI:
- Directional flow: strongly positive, dominated by 30 bps (6x weight).
- Coherence: only the 30 bps pool is clearly directional → 2x boost.
- Price impact: the 30 bps pool moved 3x more per dollar than the 5 bps → impact_asym = 3.0.
- OFI concentration: 1.0 / 0.1 = 10, capped at 5.0.
- Cluster boost: 1 + 0.86 = 1.86.
- Combined FTI: a very high score. Ranks in the 95th percentile of the past 24 hours.

**13:42:00** — Checks:
- Regime? ACTIVE (good).
- LP bias? LPs have been pulling sell-side liquidity for the last 2 minutes. Agrees with bullish FTI.
- All three gates pass.

**13:42:01** — The system places a limit buy order for ETH-PERPETUAL on Deribit.
- Size: 95th percentile × 1.5 (ACTIVE multiplier) × 100 max contracts = 142, capped at 100 contracts.
- Price: slightly below the current mid-price (maker order to save on fees).

**13:42:15** — The order fills. The system is now long 100 contracts.

**13:48:30** — 6 minutes later. The FTI drops to the 25th percentile — the informed flow has stopped.
- The system closes the position at a higher price.
- Net profit: 42 bps minus fees.

**13:48:30** — Position closed. 2-minute cooldown begins before the next trade.

---

## Part 7: Why This Strategy Has an Edge

### Structural Advantages (Can't Be Arbitraged Away)

1. **Fee-tier self-selection is baked into the protocol.** As long as Uniswap V3 has multiple fee tiers, informed traders will always have an incentive to use the expensive one for urgent trades. This isn't a bug — it's a feature of the AMM design.

2. **Cross-venue latency is physical.** Information from Arbitrum (a Layer 2 blockchain) takes real time to propagate to centralized exchanges. Even if everyone knew about this strategy, the speed-of-light delay between observing on-chain flow and acting on Deribit would still create a tradeable window.

3. **On-chain LP data has no traditional equivalent.** The ability to see exactly where sophisticated market makers are placing and removing their liquidity — and to see this in real-time, publicly — simply doesn't exist in traditional finance. There is no TradFi equivalent of watching Goldman's market-making desk reposition in real-time.

### Why Isn't Everyone Doing This?

1. **Most quant firms monitor only the highest-volume pool** (typically 5 bps). The insight that the *least active* pool (30 bps) is the *most informative* is counterintuitive.

2. **Cross-tier analysis is data-intensive.** You need to subscribe to events from three pools simultaneously, parse low-level EVM logs, compute multiple signal dimensions per time bucket, and maintain a rolling historical distribution — all in real-time.

3. **The regime filter requires domain knowledge.** Knowing *when not to trade* is harder than knowing *when to trade*. Most systems don't have explicit regime detection and blow up during volatility events.

4. **LP behavior monitoring is novel.** Analyzing Mint/Burn events for directional bias is not standard practice even among DeFi-native quantitative firms.

### Risks and Limitations

- **Adverse selection works both ways.** If the 30 bps pool becomes widely monitored, informed traders may shift to other venues or strategies. The edge erodes if the signal becomes crowded.
- **Blockchain reorganizations** can cause event replays or missed events. The system uses 1-block confirmations to minimize latency, but this means occasional false signals from reorged blocks.
- **Deribit execution risk.** In fast markets, limit orders may not fill, leaving the system watching the price move without a position.
- **The strategy is inherently low-frequency.** Trading only the top 20% of signals means 5-15 trades per day. This is by design (quality over quantity), but it means individual trades need to be meaningfully profitable.
- **Regime classification is imperfect.** The boundaries between QUIET, ACTIVE, and CHAOTIC are set by fixed thresholds. A market that oscillates near a boundary will trigger regime switches, potentially causing whipsaw.

---

## Part 8: Key Numbers at a Glance

| What | Value | Why |
|------|-------|-----|
| Signal frequency | Every 30 seconds | Balances responsiveness vs. noise |
| Trade frequency | ~5-15 per day | Only the top 20% of signals |
| Holding period | 5-30 minutes | Long enough for price to adjust, short enough to limit risk |
| 30 bps weight vs. 1 bps | 6x | Proportional to fee premium (30/5 ≈ 6) |
| Regime filter | 3 states | QUIET / ACTIVE / CHAOTIC |
| Max position hold | 30 minutes | Hard limit regardless of signal |
| Stop-loss | 35 bps (regime-adjusted) | Tighter in QUIET, wider in CHAOTIC |
| Take-profit | 50 bps | Let winners run slightly further than losers |

---

## Part 9: Glossary

| Term | Plain English |
|------|--------------|
| **bps (basis points)** | Hundredths of a percent. 30 bps = 0.30%. 1 bps = 0.01%. |
| **AMM (Automated Market Maker)** | A smart contract that automatically sets prices and fills trades. Uniswap is an AMM. No human market maker involved. |
| **Fee tier** | The commission rate charged by a Uniswap V3 pool. Higher fee = more expensive to trade. |
| **Swap** | A trade on Uniswap. You "swap" one token for another (e.g., USDC for ETH). |
| **Liquidity Provider (LP)** | Someone who deposits tokens into a Uniswap pool to earn fees. In V3, they choose a specific price range. |
| **Mint** | An LP adding liquidity to a pool. |
| **Burn** | An LP removing liquidity from a pool. |
| **Perpetual futures (perp)** | A futures contract with no expiry date. It tracks the price of an asset. Deribit offers ETH perpetuals. |
| **Toxic flow** | Trades made by informed participants. "Toxic" because they impose losses on the liquidity providers who fill them. |
| **Flow Toxicity Index (FTI)** | Our composite score measuring how "informed" the recent trading activity looks. Higher = more likely to be informed. |
| **OFI (Order Flow Imbalance)** | The ratio of buy trades minus sell trades, divided by total trades. Measures one-sidedness. |
| **Cluster ratio** | Fraction of consecutive trades that happen in rapid succession. High = someone is splitting a large order. |
| **Cross-excitation** | When activity on the expensive pool predicts subsequent activity on the cheap pool. Information cascading outward. |
| **Regime** | The current market "mood" — calm (QUIET), active (ACTIVE), or panicking (CHAOTIC). |
| **Maker order / limit order** | An order that sits on the order book waiting to be filled. Cheaper than "taker" orders that execute immediately. |
| **Arbitrum** | A Layer 2 blockchain built on Ethereum. Faster and cheaper than Ethereum mainnet. Uniswap V3 runs on it. |
