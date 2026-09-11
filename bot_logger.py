import logging
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from config import LOG_FILE


class TerminalFormatter(logging.Formatter):
    """Color only the terminal copy; file logs remain plain and searchable."""
    def __init__(self, fmt, color=False):
        super().__init__(fmt)
        self.color = color

    def format(self, record):
        text = super().format(record)
        if self.color and getattr(record, "premium_highlight", False):
            return f"\033[1;36m{text}\033[0m"
        return text


def setup_logging():
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    terminal = logging.StreamHandler()
    terminal.setFormatter(TerminalFormatter(fmt, color=sys.stderr.isatty() and "NO_COLOR" not in os.environ))

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_FILE),
            terminal
        ],
        force=True
    )


def bot_log(message, level=logging.INFO):
    logging.getLogger("cash_secured_put").log(level, message)


def log_option_fill(symbol, side, qty, price, strike, client_id):
    """Highlight actual total fill premium, never estimates or collateral.

    $500 is informational only and is not used in sizing, ranking, or entry rules.
    Buyback debits are labeled separately from opening premium credits.
    """
    total = (Decimal(str(price)) * Decimal(str(qty)) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    highlight = 0 < total <= Decimal("500.00")
    marker = "[PREMIUM <= $500] " if highlight else ""
    action, cashflow = ("SELL_TO_OPEN", "credit_received") if side == "sell" else ("BUY_TO_CLOSE", "debit_paid")
    logging.getLogger("cash_secured_put").info(
        "%sCONFIRMED FILL %s %s qty=%g fill_price=$%.4f %s=$%.2f strike_collateral=$%.2f client_order_id=%s",
        marker, action, symbol, qty, price, cashflow, total, strike * qty * 100, client_id,
        extra={"premium_highlight": highlight},
    )
