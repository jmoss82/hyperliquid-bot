"""
XYZ HIP-3 API Client

Handles all interactions with HyperLiquid for XYZ HIP-3 perpetuals.
Based on discovered API endpoints:
- meta with dex='xyz' for metadata
- metaAndAssetCtxs with dex='xyz' for current data
- l2Book with coin='xyz:COIN' for order books
- fundingHistory with coin='xyz:COIN' and startTime=0 for funding data

Order placement uses the HyperLiquid SDK with EIP-712 signing.
"""
import asyncio
import aiohttp
import requests
import json
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from collections import deque
from loguru import logger

# HyperLiquid SDK for authenticated trading
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from .models import (
    OrderBook, OrderBookLevel, FundingRecord, AssetContext,
    Position, Order, OrderSide, OrderType, OrderStatus
)


class XYZClient:
    """
    XYZ HIP-3 API Client
    
    Provides access to:
    - Market data (prices, order books)
    - Funding rates (current and historical)
    - Position management
    - Order placement (when asset IDs are configured)
    """
    
    INFO_URL = "https://api.hyperliquid.xyz/info"
    EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange"
    
    # XYZ DEX identifier for HIP-3 queries
    DEX_NAME = "xyz"
    
    def __init__(
        self,
        wallet_address: Optional[str] = None,
        private_key: Optional[str] = None,
        testnet: bool = False,
        max_orders_per_minute: int = 30,
        account_address: Optional[str] = None,
    ):
        """
        Initialize XYZ client
        
        Args:
            wallet_address: Ethereum wallet address (for trading)
            private_key: Private key for signing (for trading)
            testnet: Use testnet (not applicable for XYZ HIP-3)
            max_orders_per_minute: Rate limit for order placement
        """
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.testnet = testnet
        self.account_address = account_address or wallet_address
        
        # Cache for metadata
        self._meta_cache: Dict = {}
        self._universe_cache: List[Dict] = []
        self._last_meta_refresh: Optional[datetime] = None
        
        # Session for async requests
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Rate limiting
        self.max_orders_per_minute = max_orders_per_minute
        self._order_timestamps: deque = deque(maxlen=max_orders_per_minute)
        
        # SDK clients (initialized lazily for trading)
        self._account: Optional[Account] = None
        self._exchange: Optional[Exchange] = None
        self._info: Optional[Info] = None
        self._trading_enabled = False
        
        # Initialize trading if credentials provided
        if wallet_address and private_key:
            self._init_trading(wallet_address, private_key)
        
        logger.info(f"XYZ HIP-3 Client initialized")

    def _normalize_coin(self, coin: str) -> str:
        """Normalize coin names for comparison (e.g., xyz:SILVER vs SILVER)."""
        if not coin:
            return ""
        normalized = coin.upper().strip()
        if normalized.startswith("XYZ:"):
            normalized = normalized.split(":", 1)[1]
        if normalized.endswith("(XYZ)"):
            normalized = normalized.replace("(XYZ)", "").strip()
        return normalized
    
    def _init_trading(self, wallet_address: str, private_key: str):
        """Initialize trading capabilities with SDK"""
        try:
            # Create account from private key
            self._account = Account.from_key(private_key)
            
            # Verify address matches
            if self._account.address.lower() != wallet_address.lower():
                raise ValueError(
                    f"Private key doesn't match wallet address! "
                    f"Expected: {wallet_address}, Got: {self._account.address}"
                )
            
            # Create SDK clients
            base_url = constants.MAINNET_API_URL
            self._info = Info(base_url, skip_ws=True)
            self._exchange = Exchange(self._account, base_url)
            
            self._trading_enabled = True
            logger.info(f"Trading enabled for wallet: {self._account.address}")
            
        except Exception as e:
            logger.error(f"Failed to initialize trading: {e}")
            self._trading_enabled = False
            raise
    
    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits"""
        now = time.time()
        
        # Remove timestamps older than 60 seconds
        while self._order_timestamps and (now - self._order_timestamps[0]) > 60:
            self._order_timestamps.popleft()
        
        # Check if we're at the limit
        if len(self._order_timestamps) >= self.max_orders_per_minute:
            return False
        
        return True
    
    def _record_order(self):
        """Record an order timestamp for rate limiting"""
        self._order_timestamps.append(time.time())
    
    # ==================== SYNC API METHODS ====================
    
    def _post_sync(self, url: str, payload: dict) -> Tuple[int, Optional[dict]]:
        """Synchronous POST request"""
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                return response.status_code, response.json()
            return response.status_code, None
        except Exception as e:
            logger.error(f"Request failed: {e}")
            return 0, None
    
    def get_meta(self, force_refresh: bool = False) -> Dict:
        """
        Get XYZ metadata (universe, margin tables)
        
        Returns:
            Dict with 'universe' and 'marginTables'
        """
        if self._meta_cache and not force_refresh:
            return self._meta_cache
        
        status, data = self._post_sync(
            self.INFO_URL,
            {"type": "meta", "dex": self.DEX_NAME}
        )
        
        if status == 200 and data:
            self._meta_cache = data
            self._universe_cache = data.get("universe", [])
            self._last_meta_refresh = datetime.now(timezone.utc)
            logger.debug(f"Loaded {len(self._universe_cache)} XYZ assets")
        
        return self._meta_cache
    
    def get_universe(self) -> List[Dict]:
        """Get list of all XYZ trading pairs"""
        if not self._universe_cache:
            self.get_meta()
        return self._universe_cache
    
    def get_asset_contexts(self) -> List[AssetContext]:
        """
        Get current context for all XYZ assets
        
        Returns:
            List of AssetContext with current prices, funding, OI
        """
        status, data = self._post_sync(
            self.INFO_URL,
            {"type": "metaAndAssetCtxs", "dex": self.DEX_NAME}
        )
        
        if status != 200 or not data or len(data) < 2:
            logger.error("Failed to get asset contexts")
            return []
        
        meta, contexts = data[0], data[1]
        universe = meta.get("universe", [])
        
        results = []
        for idx, asset in enumerate(universe):
            if idx < len(contexts):
                ctx = contexts[idx]
                results.append(AssetContext(
                    coin=asset.get("name", ""),
                    index=idx,
                    mark_price=float(ctx.get("markPx") or 0),
                    oracle_price=float(ctx.get("oraclePx") or 0),
                    funding_rate=float(ctx.get("funding") or 0),
                    open_interest=float(ctx.get("openInterest") or 0),
                    premium=float(ctx.get("premium") or 0),
                ))
        
        return results
    
    def get_asset_context(self, coin: str) -> Optional[AssetContext]:
        """Get context for a specific coin"""
        contexts = self.get_asset_contexts()
        for ctx in contexts:
            if ctx.coin == coin:
                return ctx
        return None
    
    def get_order_book(self, coin: str) -> Optional[OrderBook]:
        """
        Get L2 order book for a coin
        
        Args:
            coin: Coin name (e.g., "xyz:SILVER")
            
        Returns:
            OrderBook with bids and asks
        """
        status, data = self._post_sync(
            self.INFO_URL,
            {"type": "l2Book", "coin": coin}
        )
        
        if status != 200 or not data:
            logger.error(f"Failed to get order book for {coin}")
            return None
        
        levels = data.get("levels", [])
        bids_raw = levels[0] if levels else []
        asks_raw = levels[1] if len(levels) > 1 else []
        
        bids = [
            OrderBookLevel(
                price=float(b.get("px", 0)),
                size=float(b.get("sz", 0)),
                num_orders=int(b.get("n", 1)),
            )
            for b in bids_raw
        ]
        
        asks = [
            OrderBookLevel(
                price=float(a.get("px", 0)),
                size=float(a.get("sz", 0)),
                num_orders=int(a.get("n", 1)),
            )
            for a in asks_raw
        ]
        
        return OrderBook(
            coin=data.get("coin", coin),
            timestamp=data.get("time", 0),
            bids=bids,
            asks=asks,
        )
    
    def get_funding_history(
        self, 
        coin: str, 
        limit: int = 100,
        start_time: int = 0
    ) -> List[FundingRecord]:
        """
        Get funding rate history for a coin
        
        Args:
            coin: Coin name (e.g., "xyz:SILVER")
            limit: Max records to return
            start_time: Start timestamp in ms (0 for all)
            
        Returns:
            List of FundingRecord sorted by time (newest first)
        """
        status, data = self._post_sync(
            self.INFO_URL,
            {"type": "fundingHistory", "coin": coin, "startTime": start_time}
        )
        
        if status != 200 or not isinstance(data, list):
            logger.error(f"Failed to get funding history for {coin}")
            return []
        
        records = [
            FundingRecord(
                coin=r.get("coin", coin),
                timestamp=r.get("time", 0),
                funding_rate=float(r.get("fundingRate", 0)),
                premium=float(r.get("premium", 0)),
            )
            for r in data[:limit]
        ]
        
        return records
    
    def get_current_funding_rate(self, coin: str) -> Optional[float]:
        """Get most recent funding rate for a coin"""
        ctx = self.get_asset_context(coin)
        if ctx:
            return ctx.funding_rate
        return None
    
    def get_mid_price(self, coin: str) -> Optional[float]:
        """Get current mid price for a coin"""
        book = self.get_order_book(coin)
        if book:
            return book.mid_price
        return None
    
    # ==================== ASYNC API METHODS ====================
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create async session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def _post_async(self, url: str, payload: dict) -> Tuple[int, Optional[dict]]:
        """Asynchronous POST request"""
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.post(url, json=payload, timeout=timeout) as response:
                if response.status == 200:
                    data = await response.json()
                    return response.status, data
                return response.status, None
        except Exception as e:
            logger.error(f"Async request failed: {e}")
            return 0, None
    
    async def get_order_book_async(self, coin: str) -> Optional[OrderBook]:
        """Async version of get_order_book"""
        status, data = await self._post_async(
            self.INFO_URL,
            {"type": "l2Book", "coin": coin}
        )
        
        if status != 200 or not data:
            return None
        
        levels = data.get("levels", [])
        bids_raw = levels[0] if levels else []
        asks_raw = levels[1] if len(levels) > 1 else []
        
        bids = [
            OrderBookLevel(
                price=float(b.get("px", 0)),
                size=float(b.get("sz", 0)),
                num_orders=int(b.get("n", 1)),
            )
            for b in bids_raw
        ]
        
        asks = [
            OrderBookLevel(
                price=float(a.get("px", 0)),
                size=float(a.get("sz", 0)),
                num_orders=int(a.get("n", 1)),
            )
            for a in asks_raw
        ]
        
        return OrderBook(
            coin=data.get("coin", coin),
            timestamp=data.get("time", 0),
            bids=bids,
            asks=asks,
        )
    
    async def get_multiple_order_books(self, coins: List[str]) -> Dict[str, OrderBook]:
        """Get order books for multiple coins concurrently"""
        tasks = [self.get_order_book_async(coin) for coin in coins]
        results = await asyncio.gather(*tasks)
        
        return {
            coin: book 
            for coin, book in zip(coins, results) 
            if book is not None
        }
    
    # ==================== ORDER METHODS ====================
    
    def place_limit_order(
        self,
        coin: str,
        side: OrderSide,
        price: float,
        size: float,
        reduce_only: bool = False,
        post_only: bool = True,
        dry_run: bool = True,
    ) -> Optional[Order]:
        """
        Place a limit order on XYZ HIP-3

        Args:
            coin: Coin name (e.g., "xyz:GOLD")
            side: OrderSide.BUY or OrderSide.SELL
            price: Limit price
            size: Order size
            reduce_only: If True, can only reduce position
            post_only: If True, use Alo (post-only, guarantees maker). If False, use Gtc (can take).
            dry_run: If True, don't actually place the order

        Returns:
            Order object with status, or None if failed
        """
        # Import asset IDs from config
        from config import XYZ_ASSET_IDS

        # Check if we have the asset ID
        if coin not in XYZ_ASSET_IDS:
            logger.error(f"No asset ID configured for {coin}")
            return None

        asset_id = XYZ_ASSET_IDS[coin]
        is_buy = side == OrderSide.BUY
        side_str = "BUY" if is_buy else "SELL"
        
        # Dry run mode - return mock order
        if dry_run:
            logger.info(f"[DRY RUN] Would place {side_str} {size} {coin} @ ${price:,.2f}")
            # Generate a positive mock order ID (use abs of hash or timestamp-based)
            mock_id = abs(hash(f"{coin}_{price}_{size}_{datetime.now()}"))
            return Order(
                coin=coin,
                side=side,
                order_type=OrderType.LIMIT,
                price=price,
                size=size,
                status=OrderStatus.OPEN,
                reduce_only=reduce_only,
                order_id=mock_id,
            )
        
        # Live mode - check prerequisites
        if not self._trading_enabled:
            logger.error("Trading not enabled - initialize client with wallet credentials")
            return None
        
        # Check rate limit
        if not self._check_rate_limit():
            logger.warning(f"Rate limit exceeded ({self.max_orders_per_minute}/min)")
            return None
        
        try:
            # NOTE: Leverage should already be set via set_leverage() before placing orders
            # XYZ HIP-3 uses isolated margin, leverage must be configured per-coin

            # Format price to tick size (e.g., whole dollars for XYZ100)
            price = self.format_price(coin, price)
            size = self.format_size(coin, size)

            # XYZ orders require raw API calls (SDK doesn't support XYZ in name_to_coin)
            # Build order using short field names: a, b, p, s, r, t
            # Format price with correct decimals for this asset
            price_decimals = self.get_price_decimals(coin)
            price_str = f"{price:.{price_decimals}f}"

            # Use Alo (post-only) for maker orders, Gtc for taker orders
            tif = "Alo" if post_only else "Gtc"

            order = {
                "a": asset_id,                    # asset ID (e.g., 110000 for xyz:XYZ100)
                "b": is_buy,                      # isBuy
                "p": price_str,                   # price as string with correct decimals
                "s": str(size),                   # size as string
                "r": reduce_only,                 # reduceOnly
                "t": {"limit": {"tif": tif}}      # Alo=post-only, Gtc=can cross
            }

            # Build action (no builder parameter needed for XYZ)
            action = {
                "type": "order",
                "orders": [order],
                "grouping": "na"
            }

            # Sign the action
            from hyperliquid.utils.signing import sign_l1_action

            timestamp = int(time.time() * 1000)
            expires_after = timestamp + 300000  # 5 minutes from now

            signature = sign_l1_action(
                wallet=self._account,
                action=action,
                active_pool=None,
                nonce=timestamp,
                expires_after=expires_after,
                is_mainnet=True,
            )

            # Build final payload
            payload = {
                "action": action,
                "nonce": timestamp,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": expires_after,
            }

            logger.info(f"Placing order: {side_str} {size} {coin} @ ${price:,.2f} (asset_id={asset_id})")
            logger.debug(f"Order payload: {json.dumps(payload, indent=2)}")

            # Post to exchange endpoint
            ack_start = time.perf_counter()
            response = self._exchange.post("/exchange", payload)
            ack_ms = (time.perf_counter() - ack_start) * 1000

            logger.info(f"Order response status: {response.get('status') if response else 'None'}")
            logger.info(f"Order ack latency: {ack_ms:.1f} ms")
            logger.debug(f"Full order response: {json.dumps(response, indent=2) if response else 'None'}")

            # Record for rate limiting
            self._record_order()

            # Parse response
            if response and response.get('status') == 'ok':
                # Extract order ID from response
                statuses = response.get('response', {}).get('data', {}).get('statuses', [])
                if statuses and len(statuses) > 0:
                    status_info = statuses[0]

                    if 'resting' in status_info:
                        order_id = status_info['resting'].get('oid', 0)
                        logger.success(f"Order placed: {side_str} {size} {coin} @ ${price:,.2f} (ID: {order_id})")

                        return Order(
                            coin=coin,
                            side=side,
                            order_type=OrderType.LIMIT,
                            price=price,
                            size=size,
                            status=OrderStatus.OPEN,
                            reduce_only=reduce_only,
                            order_id=order_id,
                        )
                    elif 'filled' in status_info:
                        filled_info = status_info['filled']
                        avg_price = float(filled_info.get('avgPx', price))
                        total_size = float(filled_info.get('totalSz', size))
                        logger.success(f"Order filled: {side_str} {total_size} {coin} @ ${avg_price:,.2f}")

                        return Order(
                            coin=coin,
                            side=side,
                            order_type=OrderType.LIMIT,
                            price=avg_price,
                            size=total_size,
                            status=OrderStatus.FILLED,
                            reduce_only=reduce_only,
                        )
                    elif 'error' in status_info:
                        logger.error(f"Order rejected: {status_info['error']}")
                        return None

            logger.error(f"Order failed: {response}")
            return None

        except Exception as e:
            logger.error(f"Exception placing order: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def place_order_pair(
        self,
        coin: str,
        buy_price: float,
        sell_price: float,
        size: float,
        dry_run: bool = False
    ) -> dict:
        """
        Place BUY and SELL orders atomically in a single API call.
        
        This eliminates the timing gap between sequential order placement
        that causes post-only rejections when the market moves.
        
        Args:
            coin: Coin name (e.g., "xyz:SILVER")
            buy_price: Price for the BUY order
            sell_price: Price for the SELL order
            size: Size for both orders
            dry_run: If True, don't actually place orders
            
        Returns:
            dict with keys:
                'success': bool - True if both orders accepted
                'buy': Order or None - BUY order result
                'sell': Order or None - SELL order result
                'buy_status': str - 'resting', 'filled', 'rejected', or 'error'
                'sell_status': str - 'resting', 'filled', 'rejected', or 'error'
        """
        result = {
            'success': False,
            'buy': None,
            'sell': None,
            'buy_status': 'error',
            'sell_status': 'error',
        }
        
        if not self._trading_enabled:
            logger.warning("Trading not enabled - cannot place orders")
            return result
        
        # Dry run mode
        if dry_run:
            logger.info(f"[DRY RUN] Would place pair: BUY {size} @ ${buy_price:.2f}, SELL {size} @ ${sell_price:.2f}")
            result['success'] = True
            result['buy_status'] = 'resting'
            result['sell_status'] = 'resting'
            return result
        
        try:
            from config import XYZ_ASSET_IDS
            
            if coin not in XYZ_ASSET_IDS:
                logger.error(f"No asset ID configured for {coin}")
                return result
            
            asset_id = XYZ_ASSET_IDS[coin]
            
            # Format prices and size
            buy_price = self.format_price(coin, buy_price)
            sell_price = self.format_price(coin, sell_price)
            size = self.format_size(coin, size)
            
            price_decimals = self.get_price_decimals(coin)
            buy_price_str = f"{buy_price:.{price_decimals}f}"
            sell_price_str = f"{sell_price:.{price_decimals}f}"
            
            # Build both orders
            buy_order = {
                "a": asset_id,
                "b": True,  # isBuy
                "p": buy_price_str,
                "s": str(size),
                "r": False,  # not reduce_only
                "t": {"limit": {"tif": "Alo"}}  # post-only
            }
            
            sell_order = {
                "a": asset_id,
                "b": False,  # isSell
                "p": sell_price_str,
                "s": str(size),
                "r": False,  # not reduce_only
                "t": {"limit": {"tif": "Alo"}}  # post-only
            }
            
            # Build action with BOTH orders
            action = {
                "type": "order",
                "orders": [buy_order, sell_order],  # ATOMIC: both in one call
                "grouping": "na"
            }
            
            # Sign the action
            from hyperliquid.utils.signing import sign_l1_action
            
            timestamp = int(time.time() * 1000)
            expires_after = timestamp + 300000
            
            signature = sign_l1_action(
                wallet=self._account,
                action=action,
                active_pool=None,
                nonce=timestamp,
                expires_after=expires_after,
                is_mainnet=True,
            )
            
            payload = {
                "action": action,
                "nonce": timestamp,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": expires_after,
            }
            
            logger.info(f"Placing ATOMIC pair: BUY {size} @ ${buy_price:.2f} | SELL {size} @ ${sell_price:.2f}")
            logger.debug(f"Pair payload: {json.dumps(payload, indent=2)}")
            
            # Single API call for both orders
            ack_start = time.perf_counter()
            response = self._exchange.post("/exchange", payload)
            ack_ms = (time.perf_counter() - ack_start) * 1000
            
            logger.info(f"Pair response status: {response.get('status') if response else 'None'}")
            logger.info(f"Pair ack latency: {ack_ms:.1f} ms")
            logger.debug(f"Full pair response: {json.dumps(response, indent=2) if response else 'None'}")
            
            self._record_order()
            
            # Parse response for BOTH orders
            if response and response.get('status') == 'ok':
                statuses = response.get('response', {}).get('data', {}).get('statuses', [])
                
                if len(statuses) >= 2:
                    # Parse BUY result (index 0)
                    buy_info = statuses[0]
                    if 'resting' in buy_info:
                        result['buy_status'] = 'resting'
                        result['buy'] = Order(
                            coin=coin, side=OrderSide.BUY, order_type=OrderType.LIMIT,
                            price=buy_price, size=size, status=OrderStatus.OPEN,
                            order_id=buy_info['resting'].get('oid', 0)
                        )
                        logger.success(f"BUY resting: {size} @ ${buy_price:.2f} (ID: {result['buy'].order_id})")
                    elif 'filled' in buy_info:
                        result['buy_status'] = 'filled'
                        avg_px = float(buy_info['filled'].get('avgPx', buy_price))
                        result['buy'] = Order(
                            coin=coin, side=OrderSide.BUY, order_type=OrderType.LIMIT,
                            price=avg_px, size=size, status=OrderStatus.FILLED
                        )
                        logger.success(f"BUY filled: {size} @ ${avg_px:.2f}")
                    elif 'error' in buy_info:
                        result['buy_status'] = 'rejected'
                        logger.warning(f"BUY rejected: {buy_info['error']}")
                    
                    # Parse SELL result (index 1)
                    sell_info = statuses[1]
                    if 'resting' in sell_info:
                        result['sell_status'] = 'resting'
                        result['sell'] = Order(
                            coin=coin, side=OrderSide.SELL, order_type=OrderType.LIMIT,
                            price=sell_price, size=size, status=OrderStatus.OPEN,
                            order_id=sell_info['resting'].get('oid', 0)
                        )
                        logger.success(f"SELL resting: {size} @ ${sell_price:.2f} (ID: {result['sell'].order_id})")
                    elif 'filled' in sell_info:
                        result['sell_status'] = 'filled'
                        avg_px = float(sell_info['filled'].get('avgPx', sell_price))
                        result['sell'] = Order(
                            coin=coin, side=OrderSide.SELL, order_type=OrderType.LIMIT,
                            price=avg_px, size=size, status=OrderStatus.FILLED
                        )
                        logger.success(f"SELL filled: {size} @ ${avg_px:.2f}")
                    elif 'error' in sell_info:
                        result['sell_status'] = 'rejected'
                        logger.warning(f"SELL rejected: {sell_info['error']}")
                    
                    # Success = both accepted (resting or filled)
                    result['success'] = (
                        result['buy_status'] in ('resting', 'filled') and
                        result['sell_status'] in ('resting', 'filled')
                    )
            else:
                logger.error(f"Pair order failed: {response}")
            
            return result
            
        except Exception as e:
            logger.error(f"Exception placing order pair: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return result
    
    def cancel_order(self, coin: str, order_id: int) -> bool:
        """
        Cancel an order using raw API (SDK doesn't support xyz: coins)
        
        Args:
            coin: Coin name (e.g., "xyz:SILVER")
            order_id: Order ID to cancel
            
        Returns:
            True if successful, False otherwise
        """
        if not self._trading_enabled:
            logger.warning("Trading not enabled - cannot cancel orders")
            return False
        
        try:
            # Get asset ID for this coin (same as place_limit_order)
            from config import XYZ_ASSET_IDS
            
            if coin not in XYZ_ASSET_IDS:
                logger.error(f"Cannot cancel - unknown coin: {coin}")
                return False
            asset_id = XYZ_ASSET_IDS[coin]
            
            # Build cancel action (raw API call - SDK doesn't support xyz: coins)
            action = {
                "type": "cancel",
                "cancels": [{"a": asset_id, "o": order_id}]
            }
            
            # Sign the action
            from hyperliquid.utils.signing import sign_l1_action
            
            timestamp = int(time.time() * 1000)
            expires_after = timestamp + 300000  # 5 minutes
            
            signature = sign_l1_action(
                wallet=self._account,
                action=action,
                active_pool=None,
                nonce=timestamp,
                expires_after=expires_after,
                is_mainnet=True,
            )
            
            payload = {
                "action": action,
                "nonce": timestamp,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": expires_after,
            }
            
            logger.debug(f"Cancel payload: {json.dumps(payload, indent=2)}")
            
            response = self._exchange.post("/exchange", payload)
            
            if response and response.get('status') == 'ok':
                logger.info(f"Cancelled order {order_id} for {coin} (asset {asset_id})")
                return True
            else:
                error = response.get('response', {}).get('data', {}).get('statuses', [{}])[0].get('error', 'Unknown error')
                logger.warning(f"Cancel response not ok for {order_id}: {error}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def cancel_all_orders(self, coin: str) -> int:
        """
        Cancel all orders for a coin
        
        Args:
            coin: Coin name
            
        Returns:
            Number of orders cancelled
        """
        if not self._trading_enabled:
            logger.warning("Trading not enabled - cannot cancel orders")
            return 0
        
        try:
            # Get open orders first
            open_orders = self.get_open_orders(coin)
            
            if not open_orders:
                logger.info(f"No open orders to cancel for {coin}")
                return 0
            
            cancelled = 0
            for order in open_orders:
                if self.cancel_order(coin, order.order_id):
                    cancelled += 1
            
            logger.info(f"Cancelled {cancelled}/{len(open_orders)} orders for {coin}")
            return cancelled
            
        except Exception as e:
            logger.error(f"Failed to cancel all orders for {coin}: {e}")
            return 0
    
    def get_open_orders(self, coin: Optional[str] = None) -> List[Order]:
        """
        Get open orders from XYZ DEX
        
        Args:
            coin: Optional coin filter
            
        Returns:
            List of open orders
        
        Note: Must query xyz DEX for HIP-3 orders (same as positions)
        """
        if not self._trading_enabled or not self._info:
            return []
        
        try:
            # Query xyz DEX for HIP-3 open orders
            open_orders = self._info.open_orders(self.account_address, dex=self.DEX_NAME)
            logger.debug(f"get_open_orders: queried dex={self.DEX_NAME}, found {len(open_orders)} orders")
            
            orders = []
            normalized_filter = self._normalize_coin(coin) if coin else ""

            for order_data in open_orders:
                order_coin = order_data.get('coin', '')
                normalized_order_coin = self._normalize_coin(order_coin)
                
                # Filter by coin if specified
                if normalized_filter and normalized_order_coin != normalized_filter:
                    continue
                
                orders.append(Order(
                    coin=order_coin,
                    side=OrderSide.BUY if order_data.get('side') == 'B' else OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    price=float(order_data.get('limitPx', 0)),
                    size=float(order_data.get('sz', 0)),
                    status=OrderStatus.OPEN,
                    order_id=order_data.get('oid', 0),
                ))
            
            return orders
            
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []
    
    def set_leverage(self, coin: str, leverage: int) -> bool:
        """
        Set leverage for a coin (XYZ uses isolated margin)
        
        Args:
            coin: Coin name (e.g., "xyz:XYZ100")
            leverage: Leverage value
            
        Returns:
            True if successful
        """
        if not self._trading_enabled:
            logger.warning("Trading not enabled")
            return False
        
        try:
            # Import asset IDs from config
            from config import XYZ_ASSET_IDS

            # For XYZ pairs, we need to use the asset ID
            if coin in XYZ_ASSET_IDS:
                asset_id = XYZ_ASSET_IDS[coin]
                
                # Build leverage update action using asset ID
                action = {
                    "type": "updateLeverage",
                    "asset": asset_id,
                    "isCross": False,  # Always isolated for XYZ
                    "leverage": leverage,
                }
                
                from hyperliquid.utils.signing import sign_l1_action
                import time
                
                timestamp = int(time.time() * 1000)
                expires_after = timestamp + 300000  # 5 minutes
                
                signature = sign_l1_action(
                    wallet=self._account,
                    action=action,
                    active_pool=None,
                    nonce=timestamp,
                    expires_after=expires_after,
                    is_mainnet=True,
                )
                
                payload = {
                    "action": action,
                    "nonce": timestamp,
                    "signature": signature,
                    "vaultAddress": None,
                    "expiresAfter": expires_after,
                }
                
                response = self._exchange.post("/exchange", payload)
                
                if response and response.get('status') == 'ok':
                    logger.info(f"Set leverage for {coin} (asset {asset_id}): {leverage}x (isolated)")
                    return True
                else:
                    logger.warning(f"Leverage update response: {response}")
                    # Don't fail - leverage might already be set or default is OK
                    return True
            else:
                # Standard HyperLiquid coin - use SDK method
                response = self._exchange.update_leverage(
                    leverage=leverage,
                    name=coin,
                    is_cross=False,
                )
                logger.info(f"Set leverage for {coin}: {leverage}x (isolated)")
                return True
                
        except Exception as e:
            logger.error(f"Failed to set leverage for {coin}: {e}")
            logger.warning(f"Leverage setting failed - will use account default")
            # Don't fail the bot - leverage might already be set
            return True
    
    def get_account_balance(self) -> float:
        """
        Get available USDC balance
        
        Returns:
            Available balance in USD
        """
        if not self._trading_enabled or not self._info:
            return 0.0
        
        try:
            # Query DEFAULT dex for main account balance (USDC lives here)
            # Do NOT use dex="xyz" - that only shows margin allocated to xyz positions
            user_state = self._info.user_state(self.account_address)

            # Log full structure for debugging
            logger.debug(f"User state keys: {user_state.keys() if user_state else 'None'}")
            
            # Try multiple possible locations for balance
            # 1. Standard HyperLiquid format
            margin = user_state.get('marginSummary', {})
            if margin:
                available = float(margin.get('accountValue', 0))
                if available > 0:
                    return available
            
            # 2. Cross margin details format
            cross = user_state.get('crossMarginSummary', {})
            if cross:
                available = float(cross.get('accountValue', 0))
                if available > 0:
                    return available
            
            # 3. Withdrawable balance
            withdrawable = user_state.get('withdrawable', 0)
            if withdrawable:
                return float(withdrawable)
            
            # 4. Try direct accountValue at root
            if 'accountValue' in user_state:
                return float(user_state['accountValue'])
            
            logger.warning(f"Could not find balance in user_state: {list(user_state.keys())}")
            return 0.0
            
        except Exception as e:
            logger.error(f"Failed to get account balance: {e}")
            return 0.0
    
    def get_position(self, coin: str) -> Optional[Position]:
        """
        Get current position for a coin
        
        Args:
            coin: Coin name
            
        Returns:
            Position object or None
        """
        if not self._trading_enabled or not self._info:
            return None
        
        try:
            # Query xyz dex for HIP-3 positions
            user_state = self._info.user_state(self.account_address, dex=self.DEX_NAME)
            positions = user_state.get('assetPositions', [])

            normalized_filter = self._normalize_coin(coin)
            logger.debug(f"Looking for position: coin={coin}, normalized={normalized_filter}, total_positions={len(positions)}")

            for pos_data in positions:
                pos = pos_data.get('position', {})
                pos_coin = pos.get('coin', '')
                pos_coin_normalized = self._normalize_coin(pos_coin)
                pos_size = float(pos.get('szi', 0))
                logger.debug(f"Checking position: coin={pos_coin}, normalized={pos_coin_normalized}, size={pos_size}")

                if pos_coin_normalized == normalized_filter:
                    size = float(pos.get('szi', 0))
                    entry_price = float(pos.get('entryPx', 0)) if pos.get('entryPx') else 0
                    mark_price = float(pos.get('markPx', 0))
                    liquidation_price = float(pos.get('liquidationPx', 0)) if pos.get('liquidationPx') else None
                    unrealized_pnl = float(pos.get('unrealizedPnl', 0))
                    margin_used = float(pos.get('marginUsed', 0))
                    leverage_data = pos.get('leverage', {})
                    leverage = float(leverage_data.get('value', 1)) if isinstance(leverage_data, dict) else 1.0

                    return Position(
                        coin=coin,
                        size=size,
                        entry_price=entry_price,
                        mark_price=mark_price,
                        liquidation_price=liquidation_price,
                        unrealized_pnl=unrealized_pnl,
                        margin_used=margin_used,
                        leverage=leverage,
                    )
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get position for {coin}: {e}")
            return None
    
    def check_margin_safety(
        self,
        coin: str,
        order_size_usd: float,
        leverage: float,
        margin_buffer_pct: float = 30.0,
    ) -> Tuple[bool, str]:
        """
        Check if there's enough margin for an order with safety buffer
        
        Args:
            coin: Coin to trade
            order_size_usd: Order size in USD
            leverage: Leverage being used
            margin_buffer_pct: Keep this % as buffer (default 30%)
            
        Returns:
            Tuple of (is_safe, message)
        """
        balance = self.get_account_balance()
        
        if balance <= 0:
            return False, "Could not get account balance"
        
        # Calculate required margin
        required_margin = order_size_usd / leverage
        
        # Calculate available margin after buffer
        buffer = balance * (margin_buffer_pct / 100)
        available_for_trading = balance - buffer
        
        if required_margin > available_for_trading:
            return False, (
                f"Insufficient margin: need ${required_margin:.2f}, "
                f"available ${available_for_trading:.2f} (keeping ${buffer:.2f} buffer)"
            )
        
        return True, f"Margin OK: ${required_margin:.2f} required, ${available_for_trading:.2f} available"
    
    # ==================== UTILITY METHODS ====================
    
    async def close(self):
        """Close async session"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    def get_coin_index(self, coin: str) -> Optional[int]:
        """Get the universe index for a coin"""
        universe = self.get_universe()
        for idx, asset in enumerate(universe):
            if asset.get("name") == coin:
                return idx
        return None
    
    def format_size(self, coin: str, size: float) -> float:
        """Format size to correct decimals for a coin"""
        universe = self.get_universe()
        for asset in universe:
            if asset.get("name") == coin:
                decimals = asset.get("szDecimals", 2)
                return round(size, decimals)
        return round(size, 2)

    def get_tick_size(self, coin: str) -> float:
        """
        Get tick size for a coin

        Returns:
            Tick size (e.g., 1.0 for whole dollars, 0.01 for cents)
        """
        from config.settings import XYZ_PAIRS

        if coin in XYZ_PAIRS:
            return XYZ_PAIRS[coin].tick_size

        # Fallback: check metadata
        universe = self.get_universe()
        for asset in universe:
            if asset.get("name") == coin:
                px_decimals = asset.get("pxDecimals")
                if px_decimals is not None:
                    return 10 ** (-px_decimals)
                return 1.0  # Default to whole numbers
        return 1.0

    def format_price(self, coin: str, price: float) -> float:
        """Format price to correct tick size for a coin"""
        tick_size = self.get_tick_size(coin)
        return round(price / tick_size) * tick_size

    def get_price_decimals(self, coin: str) -> int:
        """
        Get number of decimal places for price formatting

        Returns:
            Number of decimals (e.g., 2 for $108.14, 0 for $108)
        """
        from config.settings import XYZ_PAIRS

        if coin in XYZ_PAIRS:
            return XYZ_PAIRS[coin].price_decimals

        # Fallback: check metadata
        universe = self.get_universe()
        for asset in universe:
            if asset.get("name") == coin:
                return asset.get("pxDecimals", 0)
        return 0

    def get_max_leverage(self, coin: str) -> int:
        """Get max leverage for a coin"""
        universe = self.get_universe()
        for asset in universe:
            if asset.get("name") == coin:
                return asset.get("maxLeverage", 10)
        return 10
