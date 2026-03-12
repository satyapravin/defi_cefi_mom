from __future__ import annotations

import asyncio
import math
from typing import Awaitable, Callable, Optional

from web3 import AsyncWeb3, WebSocketProvider
from web3.types import LogReceipt

from config import Config, PairConfig
from database import Database
from logger import setup_logger
from models import FeeTier, LiquidityAction, LiquidityEvent, SwapEvent

logger = setup_logger()

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
MINT_TOPIC = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
BURN_TOPIC = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"

RPC_CALL_TIMEOUT = 15
HEALTH_CHECK_INTERVAL = 30


class EventListener:
    def __init__(
        self,
        config: Config,
        database: Database,
        on_swap: Callable[[SwapEvent], Awaitable[None]],
        on_liquidity: Optional[Callable[[LiquidityEvent], Awaitable[None]]] = None,
    ):
        self._config = config
        self._database = database
        self._on_swap = on_swap
        self._on_liquidity = on_liquidity
        self._w3: AsyncWeb3 | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._last_price: dict[str, float] = {}
        self._last_tick: dict[str, int] = {}

        self._pool_map: dict[str, tuple[str, FeeTier, PairConfig]] = {}
        for pair_cfg in config.pairs:
            for tier_name, pool_cfg in [
                ("bp30", pair_cfg.pools.bp30),
                ("bp5", pair_cfg.pools.bp5),
                ("bp1", pair_cfg.pools.bp1),
            ]:
                addr = pool_cfg.address.lower()
                self._pool_map[addr] = (
                    pair_cfg.name,
                    FeeTier(tier_name),
                    pair_cfg,
                )

    async def start(self) -> None:
        self._running = True
        reconnect_attempts = 0
        max_attempts = self._config.infrastructure.max_reconnect_attempts

        while self._running and reconnect_attempts <= max_attempts:
            try:
                async with AsyncWeb3(WebSocketProvider(self._config.infrastructure.rpc_url)) as w3:
                    self._w3 = w3
                    connected = await asyncio.wait_for(
                        w3.is_connected(), timeout=RPC_CALL_TIMEOUT,
                    )
                    if not connected:
                        raise ConnectionError("Failed to connect to RPC")

                    logger.info(
                        "Connected to RPC",
                        chain_id=self._config.infrastructure.chain_id,
                    )
                    reconnect_attempts = 0

                    pool_addresses = list(self._pool_map.keys())
                    checksum_addrs = [
                        AsyncWeb3.to_checksum_address(a) for a in pool_addresses
                    ]

                    swap_filter = await asyncio.wait_for(
                        w3.eth.filter({
                            "topics": [SWAP_TOPIC],
                            "address": checksum_addrs,
                        }),
                        timeout=RPC_CALL_TIMEOUT,
                    )

                    mint_burn_filter = None
                    if self._on_liquidity is not None:
                        mint_burn_filter = await asyncio.wait_for(
                            w3.eth.filter({
                                "topics": [[MINT_TOPIC, BURN_TOPIC]],
                                "address": checksum_addrs,
                            }),
                            timeout=RPC_CALL_TIMEOUT,
                        )

                    polls_since_health_check = 0
                    while self._running:
                        try:
                            swap_logs = await asyncio.wait_for(
                                swap_filter.get_new_entries(),
                                timeout=RPC_CALL_TIMEOUT,
                            )
                            for log_entry in swap_logs:
                                await self._process_swap_log(log_entry)

                            if mint_burn_filter is not None:
                                liq_logs = await asyncio.wait_for(
                                    mint_burn_filter.get_new_entries(),
                                    timeout=RPC_CALL_TIMEOUT,
                                )
                                for log_entry in liq_logs:
                                    await self._process_liquidity_log(log_entry)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "RPC poll timed out, triggering reconnect",
                                timeout=RPC_CALL_TIMEOUT,
                            )
                            raise
                        except Exception as e:
                            if not self._running:
                                break
                            logger.warning("Error polling logs", error=str(e))
                            raise

                        polls_since_health_check += 1
                        if polls_since_health_check >= HEALTH_CHECK_INTERVAL:
                            polls_since_health_check = 0
                            try:
                                alive = await asyncio.wait_for(
                                    w3.is_connected(),
                                    timeout=RPC_CALL_TIMEOUT,
                                )
                                if not alive:
                                    logger.warning("Health check failed: provider disconnected")
                                    raise ConnectionError("Provider health check failed")
                            except asyncio.TimeoutError:
                                logger.warning("Health check timed out")
                                raise ConnectionError("Provider health check timed out")

                        await asyncio.sleep(1)

            except Exception as e:
                if not self._running:
                    break
                reconnect_attempts += 1
                logger.warning(
                    "WebSocket disconnected, reconnecting",
                    attempt=reconnect_attempts,
                    error=str(e),
                )
                await self._handle_reconnect()

        if reconnect_attempts > max_attempts:
            logger.error("Max reconnect attempts exceeded")
            raise ConnectionError("Max reconnect attempts exceeded")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()

    async def _process_swap_log(self, log_entry: LogReceipt) -> None:
        pool_address = log_entry["address"].lower()
        if pool_address not in self._pool_map:
            return

        pair_name, fee_tier, pair_config = self._pool_map[pool_address]

        try:
            event = self._parse_swap_log(
                log_entry, pair_name, fee_tier, pool_address, pair_config
            )
        except Exception as e:
            logger.error("Failed to parse swap log", error=str(e), tx=log_entry.get("transactionHash", b"").hex())
            return

        self._last_tick[pool_address] = event.tick

        logger.debug(
            "swap_event",
            pair=pair_name,
            tier=fee_tier.value,
            price=event.price,
            log_return=event.log_return,
        )

        await self._database.insert_swap_event(event)
        await self._on_swap(event)

    async def _process_liquidity_log(self, log_entry: LogReceipt) -> None:
        pool_address = log_entry["address"].lower()
        if pool_address not in self._pool_map:
            return

        pair_name, fee_tier, pair_config = self._pool_map[pool_address]
        topics = log_entry.get("topics", [])
        if not topics:
            return

        topic0 = topics[0]
        if isinstance(topic0, bytes):
            topic0 = "0x" + topic0.hex()

        try:
            event = self._parse_liquidity_log(
                log_entry, topic0, pair_name, fee_tier, pool_address
            )
        except Exception as e:
            logger.error("Failed to parse liquidity log", error=str(e))
            return

        if event is None:
            return

        await self._database.insert_liquidity_event(event)
        if self._on_liquidity:
            await self._on_liquidity(event)

    def _parse_swap_log(
        self,
        log: LogReceipt,
        pair_name: str,
        fee_tier: FeeTier,
        pool_address: str,
        pair_config: PairConfig,
    ) -> SwapEvent:
        data = log["data"]
        if isinstance(data, str):
            data = bytes.fromhex(data[2:]) if data.startswith("0x") else bytes.fromhex(data)

        amount0 = int.from_bytes(data[0:32], "big", signed=True)
        amount1 = int.from_bytes(data[32:64], "big", signed=True)
        sqrt_price_x96 = int.from_bytes(data[64:96], "big", signed=False)
        liquidity = int.from_bytes(data[96:128], "big", signed=False)
        tick = int.from_bytes(data[128:160], "big", signed=True)

        price = self._compute_price(
            sqrt_price_x96,
            pair_config.token0_decimals,
            pair_config.token1_decimals,
            pair_config.invert_price,
        )

        log_return, direction = self._compute_log_return(pool_address, price)

        tx_hash = log["transactionHash"]
        if isinstance(tx_hash, bytes):
            tx_hash = tx_hash.hex()
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash

        block_number = log["blockNumber"]
        block_timestamp = 0  # Will be filled by block lookup if available

        return SwapEvent(
            pair_name=pair_name,
            fee_tier=fee_tier,
            block_number=block_number,
            block_timestamp=block_timestamp,
            transaction_hash=tx_hash,
            pool_address=pool_address,
            sqrt_price_x96=sqrt_price_x96,
            tick=tick,
            liquidity=liquidity,
            amount0=amount0,
            amount1=amount1,
            price=price,
            log_return=log_return,
            direction=direction,
        )

    @staticmethod
    def _compute_price(
        sqrt_price_x96: int,
        token0_decimals: int,
        token1_decimals: int,
        invert: bool,
    ) -> float:
        p_raw = (sqrt_price_x96 / (2**96)) ** 2
        p = p_raw * 10 ** (token0_decimals - token1_decimals)
        if invert:
            p = 1.0 / p if p != 0 else 0.0
        return p

    def _compute_log_return(
        self, pool_address: str, price: float
    ) -> tuple[float | None, int | None]:
        prev = self._last_price.get(pool_address)
        self._last_price[pool_address] = price

        if prev is None or prev <= 0 or price <= 0:
            return None, None

        r = math.log(price) - math.log(prev)
        direction = 1 if r > 0 else (-1 if r < 0 else 0)
        return r, direction

    def _parse_liquidity_log(
        self,
        log: LogReceipt,
        topic0: str,
        pair_name: str,
        fee_tier: FeeTier,
        pool_address: str,
    ) -> Optional[LiquidityEvent]:
        topics = log.get("topics", [])

        if topic0 == MINT_TOPIC:
            action = LiquidityAction.MINT
            if len(topics) < 4:
                return None
            tick_lower = self._decode_int24(topics[2])
            tick_upper = self._decode_int24(topics[3])
            data = log["data"]
            if isinstance(data, str):
                data = bytes.fromhex(data[2:]) if data.startswith("0x") else bytes.fromhex(data)
            # Mint data: sender(32) + amount(32) + amount0(32) + amount1(32)
            amount = int.from_bytes(data[32:64], "big", signed=False)
            amount0 = int.from_bytes(data[64:96], "big", signed=False)
            amount1 = int.from_bytes(data[96:128], "big", signed=False)
        elif topic0 == BURN_TOPIC:
            action = LiquidityAction.BURN
            if len(topics) < 4:
                return None
            tick_lower = self._decode_int24(topics[2])
            tick_upper = self._decode_int24(topics[3])
            data = log["data"]
            if isinstance(data, str):
                data = bytes.fromhex(data[2:]) if data.startswith("0x") else bytes.fromhex(data)
            # Burn data: amount(32) + amount0(32) + amount1(32)
            amount = int.from_bytes(data[0:32], "big", signed=False)
            amount0 = int.from_bytes(data[32:64], "big", signed=False)
            amount1 = int.from_bytes(data[64:96], "big", signed=False)
        else:
            return None

        tx_hash = log["transactionHash"]
        if isinstance(tx_hash, bytes):
            tx_hash = tx_hash.hex()
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash

        current_tick = self._last_tick.get(pool_address, 0)

        return LiquidityEvent(
            pair_name=pair_name,
            fee_tier=fee_tier,
            block_number=log["blockNumber"],
            block_timestamp=0,
            transaction_hash=tx_hash,
            pool_address=pool_address,
            action=action,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            amount=amount,
            amount0=amount0,
            amount1=amount1,
            current_tick=current_tick,
        )

    @staticmethod
    def _decode_int24(topic) -> int:
        if isinstance(topic, bytes):
            raw = topic
        else:
            s = topic if isinstance(topic, str) else str(topic)
            if s.startswith("0x"):
                s = s[2:]
            raw = bytes.fromhex(s)
        val = int.from_bytes(raw[-3:], "big", signed=False)
        if val >= 0x800000:
            val -= 0x1000000
        return val

    async def _handle_reconnect(self) -> None:
        delay = self._config.infrastructure.reconnect_delay_seconds
        await asyncio.sleep(delay)
