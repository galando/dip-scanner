"""SEC EDGAR — dated 8-K events and point-in-time fundamentals.

yfinance answers "what are this company's numbers now". Two questions the
scanner needs answered are out of its reach:

  1. Did the company just tell the SEC something that breaks the thesis?
     8-K filings carry item codes (4.02 "past financials no longer reliable",
     1.03 bankruptcy, ...) and the date they were filed, so Gate 3 can act on
     them live and the backtest can replay them without lookahead.
  2. What did the numbers look like on a past date? The companyfacts API holds
     every value each 10-Q and 10-K reported, each stamped with the date it was
     filed. Keeping only the values filed by a given day rebuilds what was
     known that day, which is what Gate 1 needs to be replayed at all.

SEC asks automated tools to identify themselves with a User-Agent naming a
person or company and an email address. Set SEC_USER_AGENT ("Name email").
Without it this module makes no request, and every caller behaves exactly as
it did before SEC support existed.

The 8-K item list and the revenue concepts follow the MIT-licensed
day-trader-skill pack (github.com/Jakeschincariol/day-trader-skill), whose
earnings reader keeps the LATEST filing of every period. That is right for
reading a company today and wrong for a backtest: here only values filed by
the as-of date count, so a later restatement cannot leak into the past.
"""
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

import config

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# SEC's fair-access limit is 10 requests a second; stay well under it.
REQUEST_PAUSE = 0.15
TIMEOUT = 30

# The 8-K items this scanner acts on, as (Hebrew, English) labels. Which are
# hard (reject) and which soft (warn) is config: SEC_8K_HARD_ITEMS / _SOFT_ITEMS.
ITEM_LABELS = {
    "1.03": ("פשיטת רגל או כינוס נכסים", "bankruptcy or receivership"),
    "2.04": ("האצת פירעון חוב", "debt acceleration"),
    "2.06": ("מחיקת ערך מהותית של נכסים", "material impairment"),
    "3.01": ("הודעה על מחיקה מהמסחר", "delisting notice"),
    "4.02": ("הדוחות הכספיים הקודמים אינם אמינים עוד", "past financials no longer reliable"),
    "5.02": ("שינוי בהנהלה או בדירקטוריון", "director or officer change"),
}

# Gate 1 inputs, in Yahoo's field names and units, so gate_1_quality reads SEC
# values without knowing where they came from.
QUALITY_FIELDS = ("returnOnEquity", "operatingMargins", "debtToEquity", "marketCap")

# Forms whose numbers count. Amendments ("10-K/A") count as their base form.
REPORT_FORMS = ("10-Q", "10-K", "10-QT", "10-KT", "20-F", "40-F")

# Concept groups. Within a group the tags are synonyms (a company that moved
# from SalesRevenueNet to the ASC 606 tag), so their quarters are merged, the
# earlier tag filling periods the later one lacks. Groups are alternatives
# with different meanings and are never mixed: the first group that yields a
# trailing twelve months wins.
REVENUE = (("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"),)
# Pre-tax income stands in when no operating income line is filed (banks,
# insurers and some conglomerates report none).
OPERATING_INCOME = (
    ("OperatingIncomeLoss",),
    ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
     "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
)
NET_INCOME = (("NetIncomeLoss",), ("NetIncomeLossAvailableToCommonStockholdersBasic",),
              ("ProfitLoss",))
EQUITY = (("StockholdersEquity",),
          ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",))
# Debt is assembled from the balance sheet; see _debt_at for how the pieces
# combine without counting any of them twice. Leases are not debt here.
DEBT_CONCEPTS = ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
                 "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
                 "LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper",
                 "LongTermDebt", "DebtLongtermAndShorttermCombinedAmount")
SHARES_DEI = "EntityCommonStockSharesOutstanding"
SHARES_DILUTED = "WeightedAverageNumberOfDilutedSharesOutstanding"


def _flat(groups) -> tuple[str, ...]:
    return tuple(c for g in groups for c in g)


# (taxonomy, concept, unit) for every fact this module reads.
NEEDED = (
    [("us-gaap", c, "USD") for c in _flat(REVENUE + OPERATING_INCOME + NET_INCOME + EQUITY)]
    + [("us-gaap", c, "USD") for c in DEBT_CONCEPTS]
    + [("dei", SHARES_DEI, "shares"), ("us-gaap", SHARES_DILUTED, "shares")]
)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def user_agent() -> str | None:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    return ua or None


_last_request = 0.0
_refused = False      # SEC answered 403: the User-Agent was rejected; stop asking


def enabled() -> bool:
    """True when SEC requests may be made: a User-Agent is set and was not refused."""
    return user_agent() is not None and not _refused


def _get_json(url: str):
    """GET one SEC JSON document. None on any failure, which is logged."""
    global _last_request, _refused
    ua = user_agent()
    if ua is None or _refused:
        return None
    for attempt in range(2):
        wait = REQUEST_PAUSE - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            resp = requests.get(url, timeout=TIMEOUT, headers={
                "User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        except requests.RequestException as e:
            logger.warning("SEC request failed (%s): %s", url, e)
            return None
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                logger.warning("SEC returned a body that is not JSON: %s", url)
                return None
        if resp.status_code == 403:
            # SEC refuses undeclared or malformed User-Agents with 403 on every
            # request. One refusal is enough to know; asking again for each of
            # 500 tickers would only add 500 warnings.
            _refused = True
            logger.error("SEC refused the request (403). Check SEC_USER_AGENT: it must "
                         "name you and give an email address. SEC data is off for this run.")
            return None
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(2.0)
            continue
        if resp.status_code != 404:
            logger.warning("SEC returned %d for %s", resp.status_code, url)
        return None
    return None


def clear_cache() -> None:
    """Forget the ticker map and any refusal, as at the start of a new run."""
    global _cik_map, _refused
    _cik_map = None
    _refused = False


# --------------------------------------------------------------------------- #
# Ticker -> CIK
# --------------------------------------------------------------------------- #
_cik_map: dict[str, int] | None = None


def _normalize(ticker: str) -> str:
    # The universe uses yfinance's BRK-B; SEC's ticker file uses the same dash.
    return ticker.strip().upper().replace(".", "-")


def parse_ticker_map(raw) -> dict[str, int]:
    """SEC's company_tickers.json -> {"AAPL": 320193, ...}."""
    out: dict[str, int] = {}
    rows = raw.values() if isinstance(raw, dict) else []
    for row in rows:
        ticker, cik = row.get("ticker"), row.get("cik_str")
        if ticker and cik is not None:
            out.setdefault(_normalize(ticker), int(cik))
    return out


def cik_for(ticker: str) -> int | None:
    global _cik_map
    if _cik_map is None:
        # Cached even when empty: a dead endpoint costs one request per run,
        # not one per stock.
        _cik_map = parse_ticker_map(_get_json(TICKERS_URL))
        if not _cik_map:
            logger.warning("SEC ticker list unavailable; no SEC data this run")
    return _cik_map.get(_normalize(ticker))


# --------------------------------------------------------------------------- #
# 8-K filings
# --------------------------------------------------------------------------- #
def _split_items(raw) -> list[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def parse_8k(blocks: list[dict]) -> list[dict]:
    """EDGAR's columnar filing blocks -> 8-K events, newest first.

    Each event is {"date": "2026-07-30", "items": ["2.02", "9.01"]}. Amended
    8-Ks count: an 8-K/A that adds a 4.02 is still that event.
    """
    out = []
    for block in blocks:
        forms = block.get("form") or []
        dates = block.get("filingDate") or []
        items = block.get("items") or []
        for i, form in enumerate(forms):
            if not str(form).startswith("8-K") or i >= len(dates):
                continue
            out.append({"date": dates[i],
                        "items": _split_items(items[i] if i < len(items) else "")})
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


def fetch_filings(cik: int, since: date | None = None) -> list[dict] | None:
    """8-K events for a company; None when SEC could not be read.

    The main submissions file holds at least the last year (or 1,000 filings).
    A backtest that starts earlier passes `since`, and the older pages that
    reach back that far are fetched too.
    """
    main = _get_json(SUBMISSIONS_URL.format(cik=cik))
    if not isinstance(main, dict):
        return None
    filings = main.get("filings") or {}
    blocks = [filings.get("recent") or {}]
    if since is not None:
        for page in filings.get("files") or []:
            if str(page.get("filingTo", "")) < since.isoformat() or not page.get("name"):
                continue
            extra = _get_json(SUBMISSIONS_PAGE_URL.format(name=page["name"]))
            if isinstance(extra, dict):
                # Older pages are the bare columnar block; accept the wrapped
                # shape too rather than silently reading nothing.
                blocks.append((extra.get("filings") or {}).get("recent") or extra)
    return parse_8k(blocks)


def filing_flags(filings: list[dict], as_of: date, cfg) -> tuple[list[dict], list[dict]]:
    """(hard, soft) 8-K events inside their lookback windows ending at as_of.

    Each item is reported once, at its newest filing. Which filings are visible
    is the caller's decision; this only decides which ones matter.
    """
    hard_items = set(getattr(cfg, "SEC_8K_HARD_ITEMS", ()))
    soft_items = set(getattr(cfg, "SEC_8K_SOFT_ITEMS", ())) - hard_items
    hard_from = (as_of - timedelta(days=getattr(cfg, "SEC_8K_HARD_LOOKBACK_DAYS", 90))).isoformat()
    soft_from = (as_of - timedelta(days=getattr(cfg, "SEC_8K_SOFT_LOOKBACK_DAYS", 30))).isoformat()
    newest: dict[str, str] = {}
    for event in filings:
        filed = event["date"]
        for item in event["items"]:
            if (item in hard_items and filed >= hard_from) or (item in soft_items and filed >= soft_from):
                if filed > newest.get(item, ""):
                    newest[item] = filed
    hard = [{"item": i, "date": d} for i, d in newest.items() if i in hard_items]
    soft = [{"item": i, "date": d} for i, d in newest.items() if i in soft_items]
    for events in (hard, soft):
        events.sort(key=lambda e: (e["date"], e["item"]), reverse=True)
    return hard, soft


def describe(event: dict) -> str:
    """One bilingual warning line for an 8-K event."""
    item = event["item"]
    he, en = ITEM_LABELS.get(item, (f"סעיף {item}", f"item {item}"))
    return (f"דיווח 8-K מתאריך {event['date']}: {he} / "
            f"8-K filed {event['date']}: {en} (item {item})")


# --------------------------------------------------------------------------- #
# Company facts
# --------------------------------------------------------------------------- #
def extract_facts(companyfacts: dict) -> dict[str, list[dict]]:
    """Keep only the concepts this module reads, and only the fields it uses.

    A large company's companyfacts file is several megabytes; a backtest holds
    one per ticker, so the rest is dropped on arrival.
    """
    facts = (companyfacts or {}).get("facts") or {}
    out: dict[str, list[dict]] = {}
    for taxonomy, concept, unit in NEEDED:
        node = (facts.get(taxonomy) or {}).get(concept)
        if not node:
            continue
        rows = (node.get("units") or {}).get(unit) or []
        kept = [{k: f.get(k) for k in ("start", "end", "val", "filed", "form", "accn")}
                for f in rows
                if f.get("filed") and f.get("end") and f.get("val") is not None
                and str(f.get("form") or "").split("/")[0] in REPORT_FORMS]
        if kept:
            out[concept] = kept
    return out


def fetch_facts(cik: int) -> dict[str, list[dict]] | None:
    raw = _get_json(FACTS_URL.format(cik=cik))
    if not isinstance(raw, dict):
        return None
    return extract_facts(raw)


@dataclass
class Company:
    ticker: str
    cik: int
    filings: list[dict]                       # 8-K events, newest first
    facts: dict[str, list[dict]] | None       # None: not requested, or SEC had none


def load_company(ticker: str, include_facts: bool = True,
                 since: date | None = None) -> Company | None:
    """Everything SEC has that the gates use, for one ticker. None when SEC is off
    or the ticker has no CIK."""
    if not enabled():
        return None
    cik = cik_for(ticker)
    if cik is None:
        if _cik_map:                 # an empty map was already reported once
            logger.info("No SEC CIK for %s", ticker)
        return None
    filings = fetch_filings(cik, since=since)
    facts = fetch_facts(cik) if include_facts else None
    return Company(ticker=ticker, cik=cik, filings=filings or [], facts=facts)


# --------------------------------------------------------------------------- #
# Point-in-time fundamentals
# --------------------------------------------------------------------------- #
def _d(s: str) -> date:
    return date.fromisoformat(s)


def _visible(facts: list[dict], known_by: str) -> list[dict]:
    return [f for f in facts if f["filed"] <= known_by]


def _latest_per_period(facts: list[dict]) -> dict[tuple, dict]:
    """{(start, end): fact}, the most recently filed value for each period.

    Applied to facts already cut at the as-of date, "most recent" means the
    latest restatement that had been published by then, never a later one.
    """
    best: dict[tuple, dict] = {}
    for f in facts:
        key = (f.get("start"), f["end"])
        stamp = (f["filed"], f.get("accn") or "")
        if key not in best or stamp > (best[key]["filed"], best[key].get("accn") or ""):
            best[key] = f
    return best


def _quarters(facts: list[dict]) -> dict[str, dict]:
    """{end: {"start", "end", "val", "derived"}} three-month values.

    Companies file no fourth-quarter 10-Q. Q4 is the 10-K year minus the three
    quarters inside it, marked derived, and only when all three are known.
    """
    per = _latest_per_period([f for f in facts if f.get("start")])
    q: dict[str, dict] = {}
    years = []
    for (start, end), f in per.items():
        days = (_d(end) - _d(start)).days
        if 80 <= days <= 100:
            q[end] = {"start": start, "end": end, "val": f["val"], "derived": False}
        elif 350 <= days <= 380:
            years.append((start, end, f["val"]))
    for start, end, val in years:
        if end in q:
            continue
        inside = sorted((v for v in q.values()
                         if not v["derived"] and v["start"] >= start and v["end"] < end),
                        key=lambda v: v["end"])
        if len(inside) != 3:
            continue
        q[end] = {"start": (_d(inside[-1]["end"]) + timedelta(days=1)).isoformat(),
                  "end": end, "val": val - sum(v["val"] for v in inside), "derived": True}
    return q


def _merged_quarters(facts: dict, group: tuple[str, ...], known_by: str) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for concept in group:
        for end, v in _quarters(_visible(facts.get(concept, []), known_by)).items():
            merged.setdefault(end, v)
    return merged


def _ttm(quarters: dict[str, dict], known_by: str, max_stale_days: int) -> tuple[float, str] | None:
    """(sum of the latest four consecutive quarters, last quarter end), or None."""
    ends = sorted(quarters)[-4:]
    if len(ends) < 4:
        return None
    span = (_d(ends[-1]) - _d(ends[0])).days
    if not 250 <= span <= 300:          # a missing quarter makes the four span a year+
        return None
    if (_d(known_by) - _d(ends[-1])).days > max_stale_days:
        return None
    return sum(quarters[e]["val"] for e in ends), ends[-1]


def _ttm_of(facts: dict, groups, known_by: str, max_stale_days: int) -> tuple[float, str] | None:
    for group in groups:
        got = _ttm(_merged_quarters(facts, group, known_by), known_by, max_stale_days)
        if got is not None:
            return got
    return None


def _instant_at(facts: dict, concept: str, end: str, known_by: str) -> float | None:
    """A balance-sheet value at exactly `end`, as most recently filed by known_by."""
    rows = [f for f in _visible(facts.get(concept, []), known_by)
            if not f.get("start") and f["end"] == end]
    if not rows:
        return None
    return float(max(rows, key=lambda f: (f["filed"], f.get("accn") or ""))["val"])


def _first_instant(facts: dict, concepts, end: str, known_by: str) -> float | None:
    for concept in concepts:
        v = _instant_at(facts, concept, end, known_by)
        if v is not None:
            return v
    return None


def _debt_at(facts: dict, end: str, known_by: str) -> float | None:
    """Total borrowings on the balance sheet dated `end`, or None if none are filed.

    Companies split the same debt across different tags. The rules, in order:
      * noncurrent long-term debt + the current part, where the current part is
        DebtCurrent when filed (it already includes short-term borrowings) or
        else current maturities + short-term borrowings;
      * otherwise DebtLongtermAndShorttermCombinedAmount;
      * otherwise LongTermDebt (which includes its current maturities) +
        short-term borrowings;
      * otherwise short-term debt alone.
    Short-term borrowings are ShortTermBorrowings, else CommercialPaper, never
    both: commercial paper is often a part of the borrowings figure.
    """
    def at(*concepts):
        return _first_instant(facts, concepts, end, known_by)

    short = at("ShortTermBorrowings", "CommercialPaper")
    noncurrent = at("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations")
    if noncurrent is not None:
        current = at("DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent")
        if current is None:
            current = (at("LongTermDebtCurrent") or 0.0) + (short or 0.0)
        return noncurrent + current
    combined = at("DebtLongtermAndShorttermCombinedAmount")
    if combined is not None:
        return combined
    total_long = at("LongTermDebt")
    if total_long is not None:
        return total_long + (short or 0.0)
    current = at("DebtCurrent")
    if current is not None:
        return current
    return short


def _shares(facts: dict, known_by: str, max_stale_days: int) -> float | None:
    """Shares outstanding from the latest cover page, summed across share classes."""
    rows = [f for f in _visible(facts.get(SHARES_DEI, []), known_by) if not f.get("start")]
    if rows:
        latest = max(rows, key=lambda f: (f["filed"], f["end"]))
        if (_d(known_by) - _d(latest["filed"])).days <= max_stale_days:
            # One cover page lists one count per share class (GOOGL has three).
            # Distinct values, because the same fact can appear twice; two
            # classes with identical counts would collapse into one, which a
            # $10B market-cap floor can live with.
            same = {f["val"] for f in rows
                    if f.get("accn") == latest.get("accn") and f["end"] == latest["end"]}
            return float(sum(same))
    per = _latest_per_period([f for f in _visible(facts.get(SHARES_DILUTED, []), known_by)
                              if f.get("start")])
    quarterly = [(end, f["val"]) for (start, end), f in per.items()
                 if 80 <= (_d(end) - _d(start)).days <= 100]
    if not quarterly:
        return None
    end, val = max(quarterly)
    if (_d(known_by) - _d(end)).days > max_stale_days:
        return None
    return float(val)


def fundamentals_asof(company: Company, known_by: date, price: float | None = None,
                      max_stale_days: int | None = None) -> dict:
    """Gate 1 inputs as they stood on `known_by`, from facts filed on or before it.

    Returns Yahoo's field names and units (fractions for ROE and margin, a
    percentage for debt/equity), each None when the filings cannot support it:
      returnOnEquity    trailing-twelve-month net income / latest equity
      operatingMargins  TTM operating income (pre-tax income if none) / TTM revenue
      debtToEquity      total borrowings / equity x 100, leases excluded
      marketCap         latest cover-page share count x `price`
    plus negativeEquity (equity <= 0 makes ROE and debt/equity meaningless) and
    periodEnd (the quarter the figures describe).
    """
    if max_stale_days is None:
        max_stale_days = config.SEC_MAX_STALENESS_DAYS
    out = {field: None for field in QUALITY_FIELDS}
    out.update({"negativeEquity": False, "periodEnd": None})
    facts = company.facts or {}
    kb = known_by.isoformat()

    net = _ttm_of(facts, NET_INCOME, kb, max_stale_days)
    revenue = _ttm_of(facts, REVENUE, kb, max_stale_days)
    operating = _ttm_of(facts, OPERATING_INCOME, kb, max_stale_days)

    # Margin needs both sums over the same four quarters.
    if revenue and operating and revenue[1] == operating[1] and revenue[0] > 0:
        out["operatingMargins"] = operating[0] / revenue[0]
        out["periodEnd"] = revenue[1]

    if net:
        end = net[1]
        equity = _first_instant(facts, _flat(EQUITY), end, kb)
        if equity is not None and equity <= 0:
            out["negativeEquity"] = True
        elif equity is not None:
            out["returnOnEquity"] = net[0] / equity
            debt = _debt_at(facts, end, kb)
            if debt is not None:
                out["debtToEquity"] = debt / equity * 100.0
        out["periodEnd"] = out["periodEnd"] or end

    if price is not None and price > 0:
        shares = _shares(facts, kb, max_stale_days)
        if shares:
            out["marketCap"] = shares * price
    return out
