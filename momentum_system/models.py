from enum import Enum
from typing import Optional

from pydantic import BaseModel


class FeeTier(str, Enum):
    BP1 = "bp1"
    BP5 = "bp5"
    BP30 = "bp30"


class Direction(int, Enum):
    LONG = 1
    SHORT = -1
    NEUTRAL = 0


class SignalTransition(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    SCALE = "scale"
    REDUCE = "reduce"
    REVERSAL = "reversal"
    NONE = "none"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PLACED = "placed"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Regime(str, Enum):
    QUIET = "quiet"
    ACTIVE = "active"
    CHAOTIC = "chaotic"


class LiquidityAction(str, Enum):
    MINT = "mint"
    BURN = "burn"


class SwapEvent(BaseModel):
    pair_name: str
    fee_tier: FeeTier
    block_number: int
    block_timestamp: int
    transaction_hash: str
    pool_address: str
    sqrt_price_x96: int
    tick: int
    liquidity: int
    amount0: int
    amount1: int
    price: float
    log_return: Optional[float] = None
    direction: Optional[int] = None


class OrderRequest(BaseModel):
    pair_name: str
    instrument: str
    direction: Direction
    size: float
    limit_price: float
    post_only: bool = True
    reduce_only: bool = False
    label: str = ""


class OrderState(BaseModel):
    order_id: str
    request: OrderRequest
    status: OrderStatus
    placed_at: float
    filled_at: Optional[float] = None
    fill_price: Optional[float] = None
    filled_size: float = 0.0
    cancel_reason: Optional[str] = None


class Position(BaseModel):
    pair_name: str
    instrument: str
    direction: Direction
    size: float
    entry_price: float
    entry_time: float
    unrealized_pnl: float = 0.0
    signal_at_entry: float = 0.0


class TradeRecord(BaseModel):
    pair_name: str
    instrument: str
    direction: Direction
    entry_price: float
    exit_price: float
    size: float
    entry_time: float
    exit_time: float
    gross_pnl_usd: float
    fees_usd: float
    net_pnl_usd: float
    signal_at_entry: float
    exit_reason: str


# --------------- Toxic Flow Oracle models ---------------


class LiquidityEvent(BaseModel):
    pair_name: str
    fee_tier: FeeTier
    block_number: int
    block_timestamp: int
    transaction_hash: str
    pool_address: str
    action: LiquidityAction
    tick_lower: int
    tick_upper: int
    amount: int
    amount0: int
    amount1: int
    current_tick: Optional[int] = None


class TradeSignal(BaseModel):
    pair_name: str
    timestamp: float
    direction: Direction
    transition: SignalTransition
    signal_strength: float
    regime: Regime
    regime_multiplier: float
    bp30_count: int
    autocorrelation: float = 0.0
    cross_tier_coherence: float = 1.0
    weighted_signal: float = 0.0
