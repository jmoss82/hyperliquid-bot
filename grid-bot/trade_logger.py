"""
Trade History Logger

Logs all trades to CSV for analysis and record-keeping.
"""
import csv
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
from loguru import logger


@dataclass
class TradeRecord:
    """Single trade record"""
    timestamp: str
    pair: str
    side: str  # BUY or SELL
    price: float
    size: float
    notional: float  # price * size
    fee: float
    cycle_profit: Optional[float] = None  # Only set when cycle completes
    running_total_pnl: float = 0.0
    position_after: float = 0.0
    notes: str = ""


class TradeLogger:
    """
    Logs all trades to CSV file

    Creates a CSV with columns:
    - Timestamp
    - Pair
    - Side (BUY/SELL)
    - Price
    - Size
    - Notional (USD value)
    - Fee
    - Cycle Profit (when sell completes a cycle)
    - Running Total P/L
    - Position After
    - Notes
    """

    def __init__(self, filename: str = "trade_history.csv"):
        """
        Initialize trade logger

        Args:
            filename: CSV filename (will be created in current directory)
        """
        self.filename = filename
        self.filepath = Path(filename)
        self.running_pnl = 0.0

        # Create file with headers if it doesn't exist
        if not self.filepath.exists():
            self._create_file()
            logger.info(f"Created trade history file: {self.filepath}")
        else:
            # If file exists, read the last running PnL to continue from there
            self._load_last_pnl()
            logger.info(f"Appending to existing trade history: {self.filepath}")

    def _create_file(self):
        """Create CSV file with headers"""
        with open(self.filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Timestamp',
                'Pair',
                'Side',
                'Price',
                'Size',
                'Notional (USD)',
                'Fee (USD)',
                'Cycle Profit (USD)',
                'Running Total P/L (USD)',
                'Position After',
                'Notes'
            ])

    def _load_last_pnl(self):
        """Load the last running PnL from existing file"""
        try:
            with open(self.filepath, 'r') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                if rows:
                    # Get last row
                    last_row = rows[-1]
                    pnl_str = last_row.get('Running Total P/L (USD)', '0')
                    self.running_pnl = float(pnl_str)
                    logger.debug(f"Continuing from running P/L: ${self.running_pnl:.2f}")
        except Exception as e:
            logger.warning(f"Could not load last PnL: {e}, starting from 0")
            self.running_pnl = 0.0

    def log_trade(
        self,
        pair: str,
        side: str,
        price: float,
        size: float,
        fee: float,
        cycle_profit: Optional[float] = None,
        position_after: float = 0.0,
        notes: str = "",
    ):
        """
        Log a trade to CSV

        Args:
            pair: Trading pair (e.g., "xyz:GOLD")
            side: "BUY" or "SELL"
            price: Execution price
            size: Order size
            fee: Fee paid
            cycle_profit: Profit if this completes a cycle (buy->sell)
            position_after: Net position after this trade
            notes: Optional notes (e.g., "DRY RUN", "Grid rebalance")
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        notional = price * size

        # Update running P/L
        if cycle_profit is not None:
            self.running_pnl += cycle_profit

        record = TradeRecord(
            timestamp=timestamp,
            pair=pair,
            side=side,
            price=price,
            size=size,
            notional=notional,
            fee=fee,
            cycle_profit=cycle_profit,
            running_total_pnl=self.running_pnl,
            position_after=position_after,
            notes=notes,
        )

        # Write to CSV
        with open(self.filepath, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                record.timestamp,
                record.pair,
                record.side,
                f"{record.price:.6f}",
                f"{record.size:.6f}",
                f"{record.notional:.2f}",
                f"{record.fee:.4f}",
                f"{record.cycle_profit:.2f}" if record.cycle_profit is not None else "",
                f"{record.running_total_pnl:.2f}",
                f"{record.position_after:+.6f}",
                record.notes,
            ])

        # Log to console
        cycle_info = f" | Cycle profit: ${cycle_profit:.2f}" if cycle_profit is not None else ""
        logger.info(
            f"Trade logged: {side} {size:.4f} {pair} @ ${price:,.2f} "
            f"(fee: ${fee:.4f}){cycle_info} | Total P/L: ${self.running_pnl:.2f}"
        )

    def get_summary(self) -> dict:
        """Get summary statistics from trade history"""
        if not self.filepath.exists():
            return {
                "total_trades": 0,
                "total_pnl": 0.0,
                "total_fees": 0.0,
                "completed_cycles": 0,
            }

        total_trades = 0
        total_fees = 0.0
        completed_cycles = 0

        with open(self.filepath, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_trades += 1
                total_fees += float(row['Fee (USD)'])
                if row['Cycle Profit (USD)']:  # Non-empty means cycle completed
                    completed_cycles += 1

        return {
            "total_trades": total_trades,
            "total_pnl": self.running_pnl,
            "total_fees": total_fees,
            "completed_cycles": completed_cycles,
        }

    def print_summary(self):
        """Print trade summary to console"""
        summary = self.get_summary()

        print(f"\n{'='*60}")
        print("Trade History Summary")
        print(f"{'='*60}")
        print(f"Total Trades:      {summary['total_trades']}")
        print(f"Completed Cycles:  {summary['completed_cycles']}")
        print(f"Total Fees Paid:   ${summary['total_fees']:.2f}")
        print(f"Net P/L:           ${summary['total_pnl']:.2f}")
        print(f"File:              {self.filepath}")
        print(f"{'='*60}\n")
