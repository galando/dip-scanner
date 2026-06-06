"""S&P 500 ticker list — fetch from Wikipedia with static fallback."""
import logging

import pandas as pd

logger = logging.getLogger(__name__)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Fallback: a representative set of S&P 500 tickers.
# Refresh occasionally from Wikipedia.
FALLBACK_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "UNH", "JNJ",
    "XOM", "JPM", "PG", "MA", "HD", "CVX", "MRK", "ABBV", "AVGO", "PEP",
    "KO", "COST", "ADBE", "WMT", "CRM", "MCD", "AMD", "NFLX", "TMO", "CSCO",
    "INTC", "QCOM", "VZ", "PFE", "NKE", "ABT", "DHR", "LLY", "MRNA", "TXN",
    "ORCL", "ACN", "CMCSA", "DIS", "GS", "BA", "CAT", "DE", "IBM", "GE",
    "RTX", "LMT", "NOW", "INTU", "SPGI", "ISRG", "BLK", "AXP", "BKNG", "CHTR",
    "AMGN", "GILD", "MDLZ", "SYK", "VRTX", "ADI", "LRCX", "REGN", "KLAC", "PANW",
    "SBUX", "MO", "TJX", "MMC", "EL", "PLD", "CB", "ICE", "CME", "CSX",
    "COP", "EOG", "SLB", "OXY", "FCX", "APD", "EMR", "ETN", "HON", "UPS",
    "FDX", "NSC", "UNP", "WM", "LIN", "SHW", "ECL", "CTAS", "FICO", "IDXX",
    "MSCI", "CDNS", "SNPS", "FTNT", "MCHP", "APH", "ANET", "MRVL", "ON", "ENPH",
]


def get_sp500_tickers() -> list[str]:
    """Fetch S&P 500 tickers from Wikipedia, fallback to static list."""
    try:
        tables = pd.read_html(WIKIPEDIA_URL)
        df = tables[0]
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        logger.info("Fetched %d tickers from Wikipedia", len(tickers))
        return tickers
    except Exception as e:
        logger.warning("Wikipedia fetch failed (%s), using fallback list of %d tickers",
                       e, len(FALLBACK_TICKERS))
        return list(FALLBACK_TICKERS)
