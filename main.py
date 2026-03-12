from __future__ import annotations

import asyncio
import signal
import sys

from config import load_config
from database import Database
from deribit_client import DeribitClient
from event_listener import EventListener
from execution_manager import ExecutionManager
from logger import setup_logger
from models import LiquidityEvent, SwapEvent
from regime_filter import RegimeFilter
from risk_manager import RiskManager
from signal_30bps import BP30SignalEngine


async def main() -> None:
    config = load_config("config.yaml")
    logger = setup_logger(config.system.log_level)
    logger.info("Starting momentum system", mode=config.system.mode)

    db = Database(config.system.database_path)
    await db.initialize()

    deribit = DeribitClient(config.deribit)
    if config.system.mode in ("live", "paper"):
        await deribit.connect()

    risk_mgr = RiskManager(config, db, deribit)
    exec_mgr = ExecutionManager(config, db, deribit, risk_mgr)

    regime_filter = RegimeFilter(config)

    risk_mgr.set_regime_filter(regime_filter)

    signal_engine = BP30SignalEngine(
        config, regime_filter, exec_mgr.on_trade_signal
    )

    exec_mgr.set_on_position_closed(signal_engine.notify_position_closed)

    async def on_swap(event: SwapEvent) -> None:
        regime_filter.on_swap(event)
        await signal_engine.on_swap(event)

    async def on_liquidity(event: LiquidityEvent) -> None:
        pass

    listener = EventListener(
        config, db, on_swap=on_swap, on_liquidity=on_liquidity
    )

    risk_mgr.set_on_exit(exec_mgr.on_forced_exit)

    if config.system.mode in ("live", "paper"):
        instruments = set(p.deribit_instrument for p in config.pairs)
        for inst in instruments:
            if "_USDC" in inst:
                currency = "USDC"
            elif "_USDT" in inst:
                currency = "USDT"
            else:
                currency = inst.split("-")[0]
            await deribit.subscribe_book(inst, exec_mgr._on_book_update)
            await deribit.subscribe_orders(inst, exec_mgr._on_order_update)
            await deribit.subscribe_trades(inst, exec_mgr._on_trade_update)
            await deribit.subscribe_portfolio(
                currency, risk_mgr._on_portfolio_update
            )

    shutdown = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    tasks: list[asyncio.Task] = []

    if config.system.mode == "backtest":
        from backtest import BacktestEngine

        logger.info("Running in backtest mode")
        bt = BacktestEngine(config, db, execution_lag_blocks=1)
        for pair_cfg in config.pairs:
            result = await bt.run_backtest(
                pair_name=pair_cfg.name,
                start_timestamp=0,
                end_timestamp=int(2e9),
            )
            logger.info(
                "Backtest result",
                pair=pair_cfg.name,
                signals=result.total_signals,
                trades=result.total_trades,
                pnl=round(result.total_pnl_usd, 2),
                sharpe=round(result.sharpe_ratio, 2),
                regime_dist=result.regime_distribution,
            )
        return

    tasks.append(asyncio.create_task(listener.start()))
    tasks.append(asyncio.create_task(risk_mgr.start_monitor()))
    tasks.append(
        asyncio.create_task(
            _heartbeat(
                config.system.heartbeat_interval_seconds,
                logger,
                config,
                regime_filter,
                exec_mgr,
                risk_mgr,
            )
        )
    )

    await shutdown.wait()

    logger.info("Shutting down")
    await listener.stop()
    await risk_mgr.stop_monitor()
    if config.system.mode in ("live", "paper"):
        await deribit.disconnect()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Shutdown complete")


async def _heartbeat(
    interval, logger, config, regime_filter, exec_mgr, risk_mgr
) -> None:
    while True:
        await asyncio.sleep(interval)
        for pair_cfg in config.pairs:
            pair = pair_cfg.name
            regime = regime_filter.get_regime(pair)
            vol = regime_filter.get_realized_vol(pair)
            intensity = regime_filter.get_intensity(pair)
            pos = exec_mgr.get_position(pair)
            logger.info(
                "heartbeat",
                pair=pair,
                regime=regime.value,
                vol=round(vol, 6),
                intensity=round(intensity, 2),
                position=pos.size if pos else 0,
                direction=pos.direction.name if pos else "NONE",
            )


if __name__ == "__main__":
    asyncio.run(main())
