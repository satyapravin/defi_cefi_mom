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
from risk_manager import RiskManager
from signal_engine import SignalEngine


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
    signal_eng = SignalEngine(config, db, on_signal=exec_mgr.on_signal)
    listener = EventListener(config, db, on_event=signal_eng.on_event)

    risk_mgr.set_on_exit(exec_mgr.on_forced_exit)

    if config.system.mode in ("live", "paper"):
        instruments = set(p.deribit_instrument for p in config.pairs)
        for inst in instruments:
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
            result = await bt.run(
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
            )
        return

    tasks.append(asyncio.create_task(listener.start()))
    tasks.append(asyncio.create_task(risk_mgr.start_monitor()))
    tasks.append(
        asyncio.create_task(
            _heartbeat(
                config.system.heartbeat_interval_seconds,
                logger,
                signal_eng,
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


async def _heartbeat(interval, logger, signal_eng, exec_mgr, risk_mgr) -> None:
    while True:
        await asyncio.sleep(interval)
        for pair in signal_eng._states:
            state = signal_eng.get_state(pair)
            pos = exec_mgr.get_position(pair)
            logger.info(
                "heartbeat",
                pair=pair,
                signal=round(state.previous_signal, 4),
                trend=state.trend_state_30,
                conviction=round(state.conviction_30, 3),
                position=pos.size if pos else 0,
                direction=pos.direction.name if pos else "NONE",
            )


if __name__ == "__main__":
    asyncio.run(main())
