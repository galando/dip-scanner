"""S&P 500 ticker list — Wikipedia fetch with cache and full static fallback.

Three layers, tried in order:

1. Live fetch from Wikipedia. Uses `requests` with a browser User-Agent —
   pandas' bare read_html(url) sends the default `Python-urllib` UA, which
   Wikipedia's CDN rejects with 403, silently shrinking the scan universe.
2. Cache of the last successful fetch (data/sp500_cache.json, committed back
   by the workflows), so one bad day at Wikipedia doesn't shrink the scan.
3. Full static snapshot of the index (~490 names) as the final fallback.

A fetched or cached list is only trusted if it has at least
MIN_EXPECTED_TICKERS entries — a partial parse must never masquerade as the
full index.
"""
import json
import logging
import os
from datetime import date
from io import StringIO

import pandas as pd
import requests

logger = logging.getLogger(__name__)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Wikipedia rejects the default Python user agents with 403; identify as a browser.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# The index has ~503 members; anything much smaller is a partial parse or a
# stale artifact and must not be trusted as "the S&P 500".
MIN_EXPECTED_TICKERS = 400

CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "data", "sp500_cache.json")

# Full static snapshot of the S&P 500 (yfinance dash format, e.g. BRK-B).
# Last resort only — the live fetch and the cache are preferred. Constituents
# drift by a handful of names per quarter; refresh this occasionally.
FALLBACK_TICKERS = [
    # Information Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "ACN", "AMD", "CSCO",
    "IBM", "INTC", "INTU", "NOW", "QCOM", "TXN", "AMAT", "PANW", "ANET", "MU",
    "LRCX", "KLAC", "SNPS", "CDNS", "APH", "MSI", "ADI", "CRWD", "FTNT", "DELL",
    "HPQ", "HPE", "NTAP", "WDC", "STX", "SMCI", "MPWR", "MCHP", "TER", "SWKS",
    "ON", "QRVO", "FSLR", "ENPH", "ZBRA", "TRMB", "GEN", "AKAM", "FFIV", "EPAM",
    "CTSH", "GLW", "IT", "KEYS", "CDW", "VRSN", "GDDY", "PLTR", "ROP", "PTC",
    "TYL", "JBL", "TDY", "WDAY", "FICO",
    # Communication Services
    "GOOGL", "GOOG", "META", "NFLX", "DIS", "CMCSA", "VZ", "T", "TMUS", "CHTR",
    "WBD", "EA", "TTWO", "OMC", "IPG", "LYV", "FOX", "FOXA", "NWS", "NWSA",
    "MTCH", "DASH", "TTD",
    # Health Care
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "DHR", "PFE", "AMGN",
    "ISRG", "BSX", "SYK", "VRTX", "GILD", "REGN", "MDT", "BMY", "ELV", "CI",
    "CVS", "ZTS", "BDX", "HCA", "MCK", "COR", "CAH", "HUM", "IDXX", "A",
    "IQV", "RMD", "GEHC", "EW", "MTD", "WAT", "STE", "DXCM", "BIIB", "MRNA",
    "WST", "BAX", "ZBH", "HOLX", "LH", "DGX", "PODD", "ALGN", "VTRS", "TECH",
    "CRL", "INCY", "MOH", "CNC", "UHS", "DVA", "SOLV", "TFX",
    # Financials
    "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "SPGI", "AXP",
    "BLK", "C", "SCHW", "MMC", "CB", "PGR", "ICE", "CME", "PYPL", "AON",
    "USB", "PNC", "COF", "TFC", "AJG", "TRV", "AFL", "ALL", "AIG", "MET",
    "PRU", "BK", "STT", "NTRS", "FITB", "HBAN", "RF", "CFG", "KEY", "MTB",
    "SYF", "AMP", "FIS", "FI", "GPN", "CPAY", "MSCI", "MCO", "NDAQ", "CBOE",
    "MKTX", "FDS", "BRO", "WRB", "CINF", "L", "GL", "AIZ", "EG", "RJF",
    "PFG", "TROW", "IVZ", "BEN", "ACGL", "HIG", "WTW", "ERIE", "KKR", "BX",
    "APO", "COIN",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "MCD", "BKNG", "NKE", "LOW", "SBUX", "TJX", "ORLY",
    "AZO", "CMG", "MAR", "HLT", "GM", "F", "RCL", "CCL", "NCLH", "LVS",
    "WYNN", "MGM", "YUM", "DRI", "DPZ", "ROST", "ULTA", "EBAY", "BBY", "DECK",
    "LULU", "GRMN", "POOL", "TSCO", "KMX", "GPC", "LKQ", "APTV", "PHM", "DHI",
    "LEN", "NVR", "MHK", "EXPE", "ABNB", "RL", "TPR", "CZR", "HAS",
    # Consumer Staples
    "PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "MDLZ", "CL", "KMB",
    "GIS", "KHC", "HSY", "SYY", "STZ", "KDP", "MNST", "ADM", "EL", "CHD",
    "CLX", "MKC", "CAG", "CPB", "HRL", "SJM", "TSN", "TAP", "LW", "BG",
    "DG", "DLTR", "TGT", "KR", "BF-B", "KVUE",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "OXY", "WMB",
    "KMI", "OKE", "HAL", "BKR", "DVN", "FANG", "CTRA", "EQT", "APA", "TRGP",
    "TPL", "EXE",
    # Industrials
    "GE", "CAT", "RTX", "HON", "UNP", "BA", "DE", "ETN", "LMT", "UPS",
    "PH", "ADP", "GD", "MMM", "ITW", "NOC", "EMR", "FDX", "CSX", "TT",
    "CARR", "TDG", "NSC", "WM", "PCAR", "JCI", "GWW", "CMI", "URI", "OTIS",
    "IR", "RSG", "AME", "FAST", "DAL", "UAL", "LUV", "ALK", "PWR", "HWM",
    "DOV", "XYL", "ROK", "EFX", "VRSK", "CTAS", "PAYX", "BR", "LDOS", "LHX",
    "HUBB", "WAB", "IEX", "SNA", "J", "TXT", "NDSN", "AOS", "PNR", "SWK",
    "CHRW", "EXPD", "JBHT", "ODFL", "ROL", "VLTO", "GNRC", "ALLE", "BLDR", "DAY",
    "PAYC", "HII", "CPRT", "UBER", "GEV", "AXON", "HEI", "MAS", "LII",
    # Materials
    "LIN", "SHW", "APD", "ECL", "FCX", "NEM", "NUE", "DOW", "DD", "PPG",
    "IP", "VMC", "MLM", "CTVA", "LYB", "PKG", "AVY", "BALL", "AMCR", "IFF",
    "ALB", "CE", "CF", "MOS", "EMN", "STLD", "SW",
    # Real Estate
    "PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "CCI", "DLR", "CBRE",
    "VICI", "EXR", "AVB", "EQR", "IRM", "VTR", "SBAC", "INVH", "MAA", "ESS",
    "KIM", "ARE", "DOC", "UDR", "CPT", "HST", "REG", "BXP", "FRT", "WY",
    "CSGP",
    # Utilities
    "NEE", "SO", "DUK", "CEG", "AEP", "SRE", "D", "EXC", "XEL", "PEG",
    "ED", "PCG", "WEC", "AWK", "DTE", "ES", "EIX", "ETR", "FE", "PPL",
    "AEE", "CMS", "CNP", "ATO", "NI", "LNT", "EVRG", "AES", "PNW", "NRG",
    "VST",
]


def _fetch_from_wikipedia() -> list[str] | None:
    """Fetch the live constituent list; None on any failure or partial parse."""
    try:
        resp = requests.get(WIKIPEDIA_URL, headers={"User-Agent": _UA}, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        tickers = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        if len(tickers) < MIN_EXPECTED_TICKERS:
            logger.warning(
                "Wikipedia parse returned only %d tickers (< %d) — not trusting it",
                len(tickers), MIN_EXPECTED_TICKERS,
            )
            return None
        logger.info("Fetched %d tickers from Wikipedia", len(tickers))
        return tickers
    except Exception as e:
        logger.warning("Wikipedia fetch failed: %s", e)
        return None


def _load_cache() -> list[str] | None:
    """Last successfully fetched list, if present and plausibly complete."""
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        tickers = data.get("tickers", [])
        if len(tickers) >= MIN_EXPECTED_TICKERS:
            logger.info("Using cached S&P 500 list (%d tickers, updated %s)",
                        len(tickers), data.get("updated", "unknown"))
            return tickers
        logger.warning("Cache has only %d tickers — ignoring it", len(tickers))
    except (FileNotFoundError, json.JSONDecodeError, TypeError) as e:
        logger.info("No usable S&P 500 cache: %s", e)
    return None


def _save_cache(tickers: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump({"updated": date.today().isoformat(), "tickers": tickers},
                      f, indent=2)
    except OSError as e:  # cache is best-effort; never fail the scan over it
        logger.warning("Could not write S&P 500 cache: %s", e)


def get_sp500_tickers() -> list[str]:
    """S&P 500 tickers: live fetch, else last good cache, else full static list."""
    tickers = _fetch_from_wikipedia()
    if tickers:
        _save_cache(tickers)
        return tickers

    cached = _load_cache()
    if cached:
        return cached

    logger.warning("Falling back to the static list of %d tickers", len(FALLBACK_TICKERS))
    return list(FALLBACK_TICKERS)
