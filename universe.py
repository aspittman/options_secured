"""Explicit research universe; membership never overrides strategy filters.

ETF identities are explicit so corporate earnings checks are not accidentally
applied to funds (or bypassed for individual stocks). Existing risk groups remain
intact; additions share a group with related holdings.
"""

ORIGINAL_ETFS = ("SPY", "QQQ", "IWM", "DIA")
ADDITIONAL_ETFS = (
    "XLF", "XLE", "XLP", "XLU", "XLI", "XLB", "XLV", "XLRE",
    "KRE", "XBI", "EEM", "EWZ", "EFA",
    "ARKK", "VNQ", "GDX", "GLD", "SLV", "TLT", "HYG", "LQD",
    "USO", "XOP", "TAN", "IGV",
)
ADDITIONAL_STOCKS = (
    "BAC", "C", "WFC", "F", "GM", "T", "VZ", "PFE", "KO", "CSCO",
    "INTC", "HPQ", "KR", "WMT", "DIS", "XOM",
    "AAPL", "AMD", "MU", "SOFI", "SNAP", "NCLH", "UBER", "PINS",
    "RIVN", "HOOD", "ROKU", "PYPL", "CVX", "OXY",
)
ETF_SYMBOLS = frozenset(ORIGINAL_ETFS + ADDITIONAL_ETFS)
DEFAULT_UNDERLYINGS = ORIGINAL_ETFS + ADDITIONAL_ETFS + ADDITIONAL_STOCKS

CORRELATION_GROUPS = {
    "index": set(ORIGINAL_ETFS),
    "technology": {"AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOG", "GOOGL", "TSLA", "NFLX", "AVGO", "CRM", "ORCL", "ADBE", "INTC", "QCOM", "MU", "CSCO", "HPQ", "ARKK", "IGV", "SNAP", "UBER", "PINS", "ROKU"},
    "financial": {"JPM", "BAC", "GS", "MS", "C", "WFC", "XLF", "KRE", "SOFI", "HOOD", "PYPL"},
    "energy": {"XOM", "CVX", "COP", "SLB", "XLE", "OXY", "USO", "XOP", "TAN"},
    "health": {"UNH", "LLY", "JNJ", "PFE", "MRK", "XLV", "XBI"},
    "consumer": {"COST", "WMT", "HD", "DIS", "BA", "F", "GM", "KO", "KR", "XLP", "XLI", "NCLH", "RIVN"},
    "utilities": {"XLU"},
    "materials": {"XLB"},
    "real_estate": {"XLRE", "VNQ"},
    "international_equity": {"EEM", "EWZ", "EFA"},
    "telecom": {"T", "VZ"},
    "precious_metals": {"GDX", "GLD", "SLV"},
    "fixed_income": {"TLT", "HYG", "LQD"},
}


def correlation_group(symbol):
    return next((group for group, symbols in CORRELATION_GROUPS.items() if symbol in symbols), symbol)


def backtest_universe(symbols):
    """No corporate history without historical earnings data; report exclusions."""
    return tuple(s for s in symbols if s in ETF_SYMBOLS), tuple(s for s in symbols if s not in ETF_SYMBOLS)
