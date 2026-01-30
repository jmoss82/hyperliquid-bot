"""
HyperLiquid Client - CCXT-based

Production-ready client with:
- Real orderbook data (not estimated)
- Automatic precision handling
- Position tracking
- TP/SL order support
- OHLCV for backtesting

Based on: https://github.com/RobotTraders/bits_and_bobs
"""
import ccxt
import pandas as pd
from typing import Optional
from dataclasses import dataclass
from enum import Enum


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class OrderResult:
    """Standardized order result"""
    order_id: str
    symbol: str
    side: str
    price: float
    size: float
    filled: float
    status: str
    raw: dict


@dataclass
class Position:
    """Current position info"""
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    unrealized_pnl: float
    liquidation_price: Optional[float]


class HyperLiquidClient:
    """
    Production-ready HyperLiquid client using CCXT.
    
    Features:
    - Real orderbook data (bid/ask spread)
    - Automatic precision handling
    - Position tracking
    - TP/SL support
    - OHLCV data for indicators
    """
    
    def __init__(self, wallet_address: str, private_key: str, testnet: bool = False):
        """
        Initialize HyperLiquid client.
        
        Args:
            wallet_address: Your HyperLiquid wallet address (0x...)
            private_key: Your wallet's private key (0x...)
            testnet: Use testnet (default: mainnet)
        """
        if not wallet_address or not wallet_address.startswith("0x"):
            raise ValueError("wallet_address must start with '0x'")
        
        if not private_key or not private_key.startswith("0x"):
            raise ValueError("private_key must start with '0x'")
        
        self.wallet_address = wallet_address
        
        # Initialize CCXT exchange
        config = {
            "walletAddress": wallet_address,
            "privateKey": private_key,
            "enableRateLimit": True,
            "options": {
                "fetchMarkets": {
                    "hip3": {
                        "dex": ["xyz"]  # Only fetch xyz DEX markets (SILVER, GOLD, XYZ100)
                    }
                }
            }
        }

        if testnet:
            config["sandbox"] = True
        
        try:
            self.exchange = ccxt.hyperliquid(config)
            self.markets = {}
            self._load_markets()
            print(f"[OK] Connected to HyperLiquid {'testnet' if testnet else 'mainnet'}")
        except Exception as e:
            raise Exception(f"Failed to initialize exchange: {e}")
    
    def _load_markets(self) -> None:
        """Load and cache market data."""
        try:
            self.markets = self.exchange.load_markets()
            print(f"[OK] Loaded {len(self.markets)} markets")
        except Exception as e:
            raise Exception(f"Failed to load markets: {e}")
    
    # ==========================================
    # PRECISION HANDLING
    # ==========================================
    
    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Format amount to exchange precision."""
        try:
            result = self.exchange.amount_to_precision(symbol, amount)
            return float(result)
        except Exception as e:
            raise Exception(f"Failed to format amount: {e}")
    
    def price_to_precision(self, symbol: str, price: float) -> float:
        """Format price to exchange precision."""
        try:
            result = self.exchange.price_to_precision(symbol, price)
            return float(result)
        except Exception as e:
            raise Exception(f"Failed to format price: {e}")
    
    def get_tick_size(self, symbol: str) -> float:
        """Get minimum price increment for symbol."""
        if symbol not in self.markets:
            raise ValueError(f"Unknown symbol: {symbol}")
        
        market = self.markets[symbol]
        return float(market.get("precision", {}).get("price", 0.01))
    
    def get_lot_size(self, symbol: str) -> float:
        """Get minimum order size for symbol."""
        if symbol not in self.markets:
            raise ValueError(f"Unknown symbol: {symbol}")
        
        market = self.markets[symbol]
        return float(market.get("precision", {}).get("amount", 0.001))
    
    # ==========================================
    # MARKET DATA - REAL ORDERBOOK
    # ==========================================
    
    def get_orderbook(self, symbol: str, limit: int = 5) -> dict:
        """
        Fetch real orderbook data.
        
        Returns:
            dict with 'bids' and 'asks' as [[price, size], ...]
        """
        try:
            return self.exchange.fetch_order_book(symbol, limit)
        except Exception as e:
            raise Exception(f"Failed to fetch orderbook: {e}")
    
    def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        """
        Get best bid and ask prices.
        
        Returns:
            (best_bid, best_ask) tuple
        """
        book = self.get_orderbook(symbol, limit=1)
        
        if not book['bids'] or not book['asks']:
            raise Exception(f"Empty orderbook for {symbol}")
        
        best_bid = float(book['bids'][0][0])
        best_ask = float(book['asks'][0][0])
        
        return best_bid, best_ask
    
    def get_mid_price(self, symbol: str) -> float:
        """Get mid price from real orderbook."""
        best_bid, best_ask = self.get_bid_ask(symbol)
        return (best_bid + best_ask) / 2
    
    def get_spread(self, symbol: str) -> tuple[float, float]:
        """
        Get spread in absolute and percentage terms.
        
        Returns:
            (spread_absolute, spread_pct)
        """
        best_bid, best_ask = self.get_bid_ask(symbol)
        spread = best_ask - best_bid
        spread_pct = spread / best_ask
        return spread, spread_pct
    
    def get_ticker(self, symbol: str) -> dict:
        """Get ticker with last price, volume, etc."""
        try:
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            raise Exception(f"Failed to fetch ticker: {e}")
    
    # ==========================================
    # OHLCV DATA (for indicators/backtesting)
    # ==========================================
    
    def fetch_ohlcv(
        self, 
        symbol: str, 
        timeframe: str = "1h",
        limit: int = 100
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candlestick data.
        
        Args:
            symbol: Trading pair
            timeframe: 1m, 5m, 15m, 30m, 1h, 4h, 12h, 1d
            limit: Number of candles
            
        Returns:
            DataFrame with timestamp, open, high, low, close, volume
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            
            df = pd.DataFrame(
                ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df = df.set_index("timestamp").sort_index()
            
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            
            return df
            
        except Exception as e:
            raise Exception(f"Failed to fetch OHLCV: {e}")
    
    # ==========================================
    # ACCOUNT & POSITIONS
    # ==========================================
    
    def fetch_balance(self) -> dict:
        """Fetch account balance."""
        try:
            return self.exchange.fetch_balance()
        except Exception as e:
            raise Exception(f"Failed to fetch balance: {e}")
    
    def get_usdc_balance(self) -> float:
        """Get available USDC balance."""
        balance = self.fetch_balance()
        return float(balance.get("free", {}).get("USDC", 0))
    
    def fetch_positions(self, symbols: Optional[list[str]] = None) -> list[Position]:
        """
        Fetch open positions.
        
        Args:
            symbols: Optional list of symbols to filter
            
        Returns:
            List of Position objects (only non-zero positions)
        """
        try:
            raw_positions = self.exchange.fetch_positions(symbols)
            
            positions = []
            for pos in raw_positions:
                size = float(pos.get("contracts", 0))
                if size == 0:
                    continue
                
                positions.append(Position(
                    symbol=pos["symbol"],
                    side=pos["side"].lower(),
                    size=abs(size),
                    entry_price=float(pos.get("entryPrice", 0)),
                    unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
                    liquidation_price=float(pos["liquidationPrice"]) if pos.get("liquidationPrice") else None
                ))
            
            return positions
            
        except Exception as e:
            raise Exception(f"Failed to fetch positions: {e}")
    
    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position for specific symbol (or None if no position)."""
        positions = self.fetch_positions([symbol])
        return positions[0] if positions else None
    
    # ==========================================
    # LEVERAGE & MARGIN
    # ==========================================
    
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol."""
        try:
            self.exchange.set_leverage(leverage, symbol)
            print(f"[OK] Leverage set to {leverage}x for {symbol}")
            return True
        except Exception as e:
            raise Exception(f"Failed to set leverage: {e}")
    
    def set_margin_mode(self, symbol: str, mode: str, leverage: int) -> bool:
        """
        Set margin mode.
        
        Args:
            symbol: Trading pair
            mode: "isolated" or "cross"
            leverage: Required for HyperLiquid
        """
        try:
            self.exchange.set_margin_mode(mode, symbol, params={"leverage": leverage})
            print(f"[OK] Margin mode set to {mode} for {symbol}")
            return True
        except Exception as e:
            raise Exception(f"Failed to set margin mode: {e}")
    
    # ==========================================
    # ORDER PLACEMENT
    # ==========================================
    
    def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        size: float,
        reduce_only: bool = False,
        post_only: bool = True
    ) -> OrderResult:
        """
        Place a limit order.
        
        Args:
            symbol: Trading pair
            side: OrderSide.BUY or OrderSide.SELL
            price: Limit price
            size: Order size
            reduce_only: Only reduce existing position
            post_only: Ensure maker fee (reject if would be taker)
            
        Returns:
            OrderResult with order details
        """
        try:
            formatted_price = self.price_to_precision(symbol, price)
            formatted_size = self.amount_to_precision(symbol, size)
            
            params = {
                "reduceOnly": reduce_only,
                "postOnly": post_only,
            }
            
            order = self.exchange.create_order(
                symbol=symbol,
                type="limit",
                side=side.value,
                amount=formatted_size,
                price=formatted_price,
                params=params
            )
            
            return OrderResult(
                order_id=order["id"],
                symbol=symbol,
                side=side.value,
                price=formatted_price,
                size=formatted_size,
                filled=float(order.get("filled", 0)),
                status=order.get("status", "unknown"),
                raw=order
            )
            
        except Exception as e:
            raise Exception(f"Failed to place limit order: {e}")
    
    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        reduce_only: bool = False
    ) -> OrderResult:
        """
        Place a market order.
        
        Args:
            symbol: Trading pair
            side: OrderSide.BUY or OrderSide.SELL
            size: Order size
            reduce_only: Only reduce existing position
            
        Returns:
            OrderResult with order details
        """
        try:
            formatted_size = self.amount_to_precision(symbol, size)
            
            # Market orders need a reference price
            mid_price = self.get_mid_price(symbol)
            formatted_price = self.price_to_precision(symbol, mid_price)
            
            params = {"reduceOnly": reduce_only}
            
            order = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=side.value,
                amount=formatted_size,
                price=formatted_price,
                params=params
            )
            
            return OrderResult(
                order_id=order["id"],
                symbol=symbol,
                side=side.value,
                price=float(order.get("average", formatted_price)),
                size=formatted_size,
                filled=float(order.get("filled", 0)),
                status=order.get("status", "unknown"),
                raw=order
            )
            
        except Exception as e:
            raise Exception(f"Failed to place market order: {e}")
    
    def place_order_with_tp_sl(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        take_profit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        order_type: str = "market"
    ) -> dict:
        """
        Place order with attached TP/SL.
        
        Args:
            symbol: Trading pair
            side: OrderSide.BUY or OrderSide.SELL
            size: Order size
            take_profit_price: TP trigger price
            stop_loss_price: SL trigger price
            order_type: "market" or "limit"
            
        Returns:
            Dict with entry, tp, and sl order results
        """
        results = {}
        
        # Place entry order
        if order_type == "market":
            entry = self.place_market_order(symbol, side, size)
        else:
            mid = self.get_mid_price(symbol)
            entry = self.place_limit_order(symbol, side, mid, size)
        
        results["entry"] = entry
        
        # Determine exit side
        exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        
        # Place TP order
        if take_profit_price:
            try:
                tp_price = self.price_to_precision(symbol, take_profit_price)
                tp_order = self.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=exit_side.value,
                    amount=self.amount_to_precision(symbol, size),
                    price=tp_price,
                    params={
                        "takeProfitPrice": tp_price,
                        "reduceOnly": True
                    }
                )
                results["take_profit"] = tp_order
            except Exception as e:
                print(f"[WARN] Failed to place TP: {e}")
        
        # Place SL order
        if stop_loss_price:
            try:
                sl_price = self.price_to_precision(symbol, stop_loss_price)
                sl_order = self.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=exit_side.value,
                    amount=self.amount_to_precision(symbol, size),
                    price=sl_price,
                    params={
                        "stopLossPrice": sl_price,
                        "reduceOnly": True
                    }
                )
                results["stop_loss"] = sl_order
            except Exception as e:
                print(f"[WARN] Failed to place SL: {e}")
        
        return results
    
    # ==========================================
    # ORDER MANAGEMENT
    # ==========================================
    
    def get_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        """Fetch open orders."""
        try:
            return self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            raise Exception(f"Failed to fetch open orders: {e}")
    
    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an order by ID."""
        try:
            self.exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            raise Exception(f"Failed to cancel order: {e}")
    
    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        Cancel all open orders.
        
        Returns:
            Number of orders cancelled
        """
        try:
            orders = self.get_open_orders(symbol)
            count = 0
            
            for order in orders:
                try:
                    self.cancel_order(order["id"], order["symbol"])
                    count += 1
                except:
                    pass
            
            return count
            
        except Exception as e:
            raise Exception(f"Failed to cancel orders: {e}")
    
    def get_order_status(self, order_id: str, symbol: str) -> dict:
        """Get order status and fill info."""
        try:
            return self.exchange.fetch_order(order_id, symbol)
        except Exception as e:
            raise Exception(f"Failed to fetch order: {e}")
    
    def get_trades(self, symbol: str, limit: int = 50) -> list[dict]:
        """Fetch recent trades (fills) for symbol."""
        try:
            return self.exchange.fetch_my_trades(symbol, limit=limit)
        except Exception as e:
            raise Exception(f"Failed to fetch trades: {e}")


# ==========================================
# CONVENIENCE FUNCTIONS
# ==========================================

def create_client_from_env() -> HyperLiquidClient:
    """Create client from environment variables."""
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    wallet = os.environ.get("HL_WALLET_ADDRESS", "")
    key = os.environ.get("HL_PRIVATE_KEY", "")
    
    if not wallet or not key:
        raise ValueError(
            "Missing credentials. Set HL_WALLET_ADDRESS and HL_PRIVATE_KEY "
            "in .env file or environment"
        )
    
    return HyperLiquidClient(wallet, key)


# ==========================================
# TEST
# ==========================================

if __name__ == "__main__":
    print("="*60)
    print("HyperLiquid Client Test")
    print("="*60)
    
    # Test without credentials (just market data)
    try:
        # Create a read-only client for testing
        # Note: Real trading requires valid credentials
        exchange = ccxt.hyperliquid({"enableRateLimit": True})
        markets = exchange.load_markets()
        
        print(f"\n[OK] Connected to HyperLiquid")
        print(f"[OK] Loaded {len(markets)} markets")
        
        # Test orderbook
        test_symbol = "ETH/USDC:USDC"
        book = exchange.fetch_order_book(test_symbol, limit=5)
        
        if book['bids'] and book['asks']:
            best_bid = book['bids'][0][0]
            best_ask = book['asks'][0][0]
            spread = best_ask - best_bid
            spread_pct = (spread / best_ask) * 100
            
            print(f"\n{test_symbol} Orderbook:")
            print(f"  Best Bid: ${best_bid:.2f}")
            print(f"  Best Ask: ${best_ask:.2f}")
            print(f"  Spread:   ${spread:.4f} ({spread_pct:.4f}%)")
            print(f"  Mid:      ${(best_bid + best_ask)/2:.2f}")
        
        print("\n[OK] Client test passed!")
        print("="*60)
        
    except Exception as e:
        print(f"\n[ERROR] {e}")
        print("\nTo test with full functionality, set credentials in .env:")
        print("  HL_WALLET_ADDRESS=0x...")
        print("  HL_PRIVATE_KEY=0x...")
