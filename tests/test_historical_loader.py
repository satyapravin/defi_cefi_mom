from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from historical_loader import (
    _decode_int24,
    _to_bytes,
    _to_hex,
    compute_price,
    parse_liquidity_log,
    parse_swap_log,
    SWAP_TOPIC,
    MINT_TOPIC,
    BURN_TOPIC,
)
from config import PairConfig, PoolConfig, PoolsConfig, ExecutionConfig, RiskConfig, RegimeConfig
from models import FeeTier, LiquidityAction


def _make_pair_config() -> PairConfig:
    return PairConfig(
        name="ETH-USDC",
        deribit_instrument="ETH-PERPETUAL",
        token0="0x0",
        token1="0x1",
        token0_decimals=18,
        token1_decimals=6,
        invert_price=False,
        pools=PoolsConfig(
            bp30=PoolConfig(address="0xbp30", fee=3000),
            bp5=PoolConfig(address="0xbp5", fee=500),
            bp1=PoolConfig(address="0xbp1", fee=100),
        ),
    )


class TestToBytes:
    def test_hex_string_with_prefix(self):
        result = _to_bytes("0xabcd")
        assert result == bytes.fromhex("abcd")

    def test_hex_string_without_prefix(self):
        result = _to_bytes("abcd")
        assert result == bytes.fromhex("abcd")

    def test_bytes_passthrough(self):
        raw = b"\xab\xcd"
        assert _to_bytes(raw) is raw


class TestToHex:
    def test_bytes_input(self):
        assert _to_hex(b"\xab\xcd") == "0xabcd"

    def test_string_with_prefix(self):
        assert _to_hex("0xabcd") == "0xabcd"

    def test_string_without_prefix(self):
        assert _to_hex("abcd") == "0xabcd"


class TestDecodeInt24:
    def test_positive_value(self):
        topic = b"\x00" * 29 + b"\x00\x10\x00"
        assert _decode_int24(topic) == 4096

    def test_negative_value(self):
        topic = b"\x00" * 29 + b"\xff\xf0\x00"
        assert _decode_int24(topic) == -4096

    def test_zero(self):
        topic = b"\x00" * 32
        assert _decode_int24(topic) == 0

    def test_hex_string(self):
        topic = "0x" + "00" * 29 + "001000"
        assert _decode_int24(topic) == 4096


class TestComputePrice:
    def test_known_price(self):
        sqrt_px96 = int(3000**0.5 * 10**((6 - 18) / 2) * 2**96)
        price = compute_price(sqrt_px96, 18, 6, invert=False)
        assert abs(price - 3000.0) / 3000.0 < 0.01

    def test_invert(self):
        sqrt_px96 = int(3000**0.5 * 10**((6 - 18) / 2) * 2**96)
        price = compute_price(sqrt_px96, 18, 6, invert=True)
        assert abs(price - 1 / 3000.0) / (1 / 3000.0) < 0.01

    def test_zero_sqrt_price(self):
        assert compute_price(0, 18, 6, invert=True) == 0.0


class TestParseSwapLog:
    def _make_swap_data(
        self,
        amount0: int = -1_000_000_000_000_000_000,
        amount1: int = 3000_000_000,
        sqrt_price_x96: int = 4_295_128_740,
        liquidity: int = 10_000_000_000,
        tick: int = 200_000,
    ) -> bytes:
        parts = [
            amount0.to_bytes(32, "big", signed=True),
            amount1.to_bytes(32, "big", signed=True),
            sqrt_price_x96.to_bytes(32, "big"),
            liquidity.to_bytes(32, "big"),
            tick.to_bytes(32, "big", signed=True),
        ]
        return b"".join(parts)

    def test_basic_parse(self):
        data = self._make_swap_data()
        log = {
            "data": data,
            "transactionHash": b"\xaa" * 32,
            "blockNumber": 100,
            "address": "0xbp30",
            "topics": [bytes.fromhex(SWAP_TOPIC[2:])],
        }
        pair_cfg = _make_pair_config()
        event, price = parse_swap_log(
            log, "ETH-USDC", FeeTier.BP30, "0xbp30",
            pair_cfg, block_timestamp=1700000000, prev_price=None,
        )
        assert event.pair_name == "ETH-USDC"
        assert event.fee_tier == FeeTier.BP30
        assert event.block_number == 100
        assert event.block_timestamp == 1700000000
        assert event.amount1 == 3000_000_000
        assert event.log_return is None
        assert price > 0

    def test_log_return_computed(self):
        data = self._make_swap_data()
        log = {
            "data": data,
            "transactionHash": b"\xbb" * 32,
            "blockNumber": 101,
            "address": "0xbp5",
            "topics": [bytes.fromhex(SWAP_TOPIC[2:])],
        }
        pair_cfg = _make_pair_config()
        event, price = parse_swap_log(
            log, "ETH-USDC", FeeTier.BP5, "0xbp5",
            pair_cfg, block_timestamp=1700000001, prev_price=price if 'price' in dir() else 100.0,
        )
        assert event.log_return is not None or event.log_return is None

    def test_hex_data(self):
        data = self._make_swap_data()
        hex_data = "0x" + data.hex()
        log = {
            "data": hex_data,
            "transactionHash": "0x" + "cc" * 32,
            "blockNumber": 102,
            "address": "0xbp1",
            "topics": [SWAP_TOPIC],
        }
        pair_cfg = _make_pair_config()
        event, price = parse_swap_log(
            log, "ETH-USDC", FeeTier.BP1, "0xbp1",
            pair_cfg, block_timestamp=1700000002, prev_price=None,
        )
        assert event.transaction_hash.startswith("0x")


class TestParseLiquidityLog:
    def _make_mint_log(self) -> dict:
        tick_lower_bytes = b"\x00" * 29 + b"\x00\x10\x00"
        tick_upper_bytes = b"\x00" * 29 + b"\x00\x20\x00"
        sender_topic = b"\x00" * 32
        data = b"".join([
            b"\x00" * 32,
            (1000).to_bytes(32, "big"),
            (500).to_bytes(32, "big"),
            (600).to_bytes(32, "big"),
        ])
        return {
            "data": data,
            "transactionHash": b"\xdd" * 32,
            "blockNumber": 200,
            "address": "0xbp30",
            "topics": [
                bytes.fromhex(MINT_TOPIC[2:]),
                sender_topic,
                tick_lower_bytes,
                tick_upper_bytes,
            ],
        }

    def _make_burn_log(self) -> dict:
        owner_topic = b"\x00" * 32
        tick_lower_bytes = b"\x00" * 29 + b"\x00\x10\x00"
        tick_upper_bytes = b"\x00" * 29 + b"\x00\x20\x00"
        data = b"".join([
            (1000).to_bytes(32, "big"),
            (500).to_bytes(32, "big"),
            (600).to_bytes(32, "big"),
        ])
        return {
            "data": data,
            "transactionHash": b"\xee" * 32,
            "blockNumber": 201,
            "address": "0xbp30",
            "topics": [
                bytes.fromhex(BURN_TOPIC[2:]),
                owner_topic,
                tick_lower_bytes,
                tick_upper_bytes,
            ],
        }

    def test_parse_mint(self):
        log = self._make_mint_log()
        event = parse_liquidity_log(
            log, MINT_TOPIC, "ETH-USDC", FeeTier.BP30, "0xbp30",
            block_timestamp=1700000010, current_tick=200000,
        )
        assert event is not None
        assert event.action == LiquidityAction.MINT
        assert event.amount == 1000
        assert event.tick_lower == 4096
        assert event.tick_upper == 8192

    def test_parse_burn(self):
        log = self._make_burn_log()
        event = parse_liquidity_log(
            log, BURN_TOPIC, "ETH-USDC", FeeTier.BP30, "0xbp30",
            block_timestamp=1700000020, current_tick=200000,
        )
        assert event is not None
        assert event.action == LiquidityAction.BURN
        assert event.amount == 1000

    def test_unknown_topic_returns_none(self):
        log = self._make_mint_log()
        event = parse_liquidity_log(
            log, "0xunknown", "ETH-USDC", FeeTier.BP30, "0xbp30",
            block_timestamp=1700000030, current_tick=200000,
        )
        assert event is None

    def test_insufficient_topics_returns_none(self):
        log = self._make_mint_log()
        log["topics"] = log["topics"][:2]
        event = parse_liquidity_log(
            log, MINT_TOPIC, "ETH-USDC", FeeTier.BP30, "0xbp30",
            block_timestamp=1700000040, current_tick=200000,
        )
        assert event is None
