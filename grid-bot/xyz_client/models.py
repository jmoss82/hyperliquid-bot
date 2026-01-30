"""
Data models for XYZ HIP-3 Client
"""
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
from enum import Enum


class OrderSide(Enum):
    """Order side"""
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """Order type"""
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(Enum):
    """Order status"""
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class OrderBookLevel:
    """Single level in order book"""
    price: float
    size: float
    num_orders: int = 1
    
    @property
    def notional(self) -> float:
        """Notional value at this level"""
        return self.price * self.size


@dataclass
class OrderBook:
    """Order book snapshot"""
    coin: str
    timestamp: int  # Unix ms
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    
    @property
    def best_bid(self) -> Optional[float]:
        """Best bid price"""
        return self.bids[0].price if self.bids else None
    
    @property
    def best_ask(self) -> Optional[float]:
        """Best ask price"""
        return self.asks[0].price if self.asks else None
    
    @property
    def mid_price(self) -> Optional[float]:
        """Mid price"""
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None
    
    @property
    def spread(self) -> Optional[float]:
        """Spread in absolute terms"""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None
    
    @property
    def spread_pct(self) -> Optional[float]:
        """Spread as percentage of mid price"""
        if self.mid_price and self.spread:
            return (self.spread / self.mid_price) * 100
        return None
    
    def bid_depth(self, levels: int = 5) -> float:
        """Total bid depth (notional) for top N levels"""
        return sum(b.notional for b in self.bids[:levels])
    
    def ask_depth(self, levels: int = 5) -> float:
        """Total ask depth (notional) for top N levels"""
        return sum(a.notional for a in self.asks[:levels])


@dataclass
class FundingRecord:
    """Single funding rate record"""
    coin: str
    timestamp: int  # Unix ms
    funding_rate: float  # Raw rate (not percentage)
    premium: float = 0.0
    
    @property
    def funding_rate_pct(self) -> float:
        """Funding rate as percentage"""
        return self.funding_rate * 100
    
    @property
    def datetime(self) -> datetime:
        """Timestamp as datetime"""
        return datetime.fromtimestamp(self.timestamp / 1000)
    
    @property
    def annualized_rate(self) -> float:
        """Annualized rate assuming hourly funding"""
        return self.funding_rate * 24 * 365 * 100  # As percentage


@dataclass
class AssetContext:
    """Current asset context (price, funding, OI)"""
    coin: str
    index: int
    mark_price: float
    oracle_price: float = 0.0
    funding_rate: float = 0.0  # Current period funding
    open_interest: float = 0.0
    premium: float = 0.0
    
    @property
    def funding_rate_pct(self) -> float:
        """Funding rate as percentage"""
        return self.funding_rate * 100


@dataclass
class Position:
    """Trading position"""
    coin: str
    size: float  # Positive = long, negative = short
    entry_price: float
    mark_price: float
    liquidation_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0
    leverage: float = 1.0
    
    @property
    def is_long(self) -> bool:
        return self.size > 0
    
    @property
    def is_short(self) -> bool:
        return self.size < 0
    
    @property
    def notional(self) -> float:
        """Position notional value"""
        return abs(self.size) * self.mark_price
    
    @property
    def pnl_pct(self) -> float:
        """PnL as percentage of entry"""
        if self.entry_price == 0:
            return 0
        if self.is_long:
            return ((self.mark_price - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - self.mark_price) / self.entry_price) * 100


@dataclass
class Order:
    """Trading order"""
    coin: str
    order_id: Optional[int] = None
    client_order_id: Optional[str] = None
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    size: float = 0.0
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    timestamp: Optional[int] = None
    reduce_only: bool = False
    
    @property
    def remaining_size(self) -> float:
        return self.size - self.filled_size
    
    @property
    def is_buy(self) -> bool:
        return self.side == OrderSide.BUY
    
    @property
    def is_sell(self) -> bool:
        return self.side == OrderSide.SELL
    
    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class GridLevel:
    """Single grid level"""
    index: int
    price: float
    size: float
    side: OrderSide
    order: Optional[Order] = None
    
    @property
    def is_filled(self) -> bool:
        return self.order is not None and self.order.status == OrderStatus.FILLED
    
    @property
    def is_open(self) -> bool:
        return self.order is not None and self.order.status == OrderStatus.OPEN


@dataclass
class GridState:
    """Current state of a grid"""
    coin: str
    center_price: float
    levels: List[GridLevel] = field(default_factory=list)
    gap_index: Optional[int] = None  # Grid level without an order (for fixed-range grids)
    
    # Tracking
    total_buys: int = 0
    total_sells: int = 0
    total_profit: float = 0.0
    
    @property
    def buy_levels(self) -> List[GridLevel]:
        return [l for l in self.levels if l.side == OrderSide.BUY]
    
    @property
    def sell_levels(self) -> List[GridLevel]:
        return [l for l in self.levels if l.side == OrderSide.SELL]
    
    @property
    def open_buy_orders(self) -> int:
        return sum(1 for l in self.buy_levels if l.is_open)
    
    @property
    def open_sell_orders(self) -> int:
        return sum(1 for l in self.sell_levels if l.is_open)
