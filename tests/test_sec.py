"""Tests for sec.py — 8-K events and point-in-time fundamentals.

The AAPL fixtures in tests/fixtures/sec/ are real SEC responses, trimmed (public
data, as shipped in the day-trader-skill pack's tests). The numbers asserted on
them were cross-checked against that pack's own independent earnings reader,
which computes the same trailing twelve months from the same file.
"""
import json
import os
from datetime import date

import pytest

import config
import src.data as data
import src.sec as sec

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "sec")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def _dur(start, end, val, filed, form="10-Q", accn=None):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form,
            "accn": accn or f"acc-{filed}"}


def _inst(end, val, filed, form="10-Q", accn=None):
    return {"end": end, "val": val, "filed": filed, "form": form, "accn": accn or f"acc-{filed}"}


def _year(y, q1, q2, q3, fy):
    """A calendar fiscal year: three 10-Q quarters and the 10-K year."""
    return [
        _dur(f"{y}-01-01", f"{y}-03-31", q1, f"{y}-05-01"),
        _dur(f"{y}-04-01", f"{y}-06-30", q2, f"{y}-08-01"),
        _dur(f"{y}-07-01", f"{y}-09-30", q3, f"{y}-11-01"),
        _dur(f"{y}-01-01", f"{y}-12-31", fy, f"{y + 1}-02-15", form="10-K"),
    ]


def _company(**facts):
    return sec.Company(ticker="TST", cik=1, filings=[], facts=facts)


def _healthy(**extra):
    """Two years of a healthy company, plus any extra concepts."""
    facts = {
        "Revenues": _year(2024, 100, 100, 100, 400) + _year(2025, 110, 110, 110, 440),
        "OperatingIncomeLoss": _year(2024, 20, 20, 20, 80) + _year(2025, 22, 22, 22, 88),
        "NetIncomeLoss": _year(2024, 15, 15, 15, 60) + _year(2025, 16, 16, 16, 64),
        "StockholdersEquity": [_inst("2025-09-30", 200, "2025-11-01"),
                               _inst("2025-12-31", 250, "2026-02-15", form="10-K")],
        "LongTermDebtNoncurrent": [_inst("2025-12-31", 100, "2026-02-15", form="10-K")],
        "LongTermDebtCurrent": [_inst("2025-12-31", 10, "2026-02-15", form="10-K")],
        "EntityCommonStockSharesOutstanding": [_inst("2026-02-01", 1000, "2026-02-15", form="10-K")],
    }
    facts.update(extra)
    return _company(**facts)


class TestPointInTime:
    def test_real_aapl_ttm_matches_an_independent_reader(self):
        company = sec.Company("AAPL", 320193, [], sec.extract_facts(_load("companyfacts-AAPL.json")))
        rev = sec._ttm_of(company.facts, sec.REVENUE, "2026-08-01", 200)
        net = sec._ttm_of(company.facts, sec.NET_INCOME, "2026-08-01", 200)
        assert rev == (466_823_000_000, "2026-06-27")
        assert net == (128_930_000_000, "2026-06-27")

    def test_a_filing_is_invisible_until_the_day_it_was_filed(self):
        """AAPL's June quarter 10-Q was filed 2026-07-31. The day before, the
        latest known quarter is March."""
        company = sec.Company("AAPL", 320193, [], sec.extract_facts(_load("companyfacts-AAPL.json")))
        assert sec._ttm_of(company.facts, sec.REVENUE, "2026-07-30", 200)[1] == "2026-03-28"
        assert sec._ttm_of(company.facts, sec.REVENUE, "2026-07-31", 200)[1] == "2026-06-27"

    def test_a_later_restatement_does_not_leak_into_the_past(self):
        q = [_dur("2025-01-01", "2025-03-31", 100, "2025-05-01"),
             _dur("2025-01-01", "2025-03-31", 80, "2026-05-01")]   # restated a year later
        per_2025 = sec._quarters(sec._visible(q, "2025-12-31"))
        per_2026 = sec._quarters(sec._visible(q, "2026-06-01"))
        assert per_2025["2025-03-31"]["val"] == 100
        assert per_2026["2025-03-31"]["val"] == 80

    def test_q4_is_derived_only_when_all_three_quarters_are_known(self):
        q = sec._quarters(_year(2025, 110, 110, 110, 440))
        assert q["2025-12-31"] == {"start": "2025-10-01", "end": "2025-12-31",
                                   "val": 110, "derived": True}
        missing_q2 = [f for f in _year(2025, 110, 110, 110, 440) if f["end"] != "2025-06-30"]
        assert "2025-12-31" not in sec._quarters(missing_q2)

    def test_ttm_needs_four_consecutive_recent_quarters(self):
        q = sec._quarters(_year(2025, 110, 110, 110, 440))
        assert sec._ttm(q, "2026-03-01", 200) == (440, "2025-12-31")
        assert sec._ttm(q, "2026-09-01", 200) is None       # latest quarter 244 days old
        del q["2025-06-30"]
        q["2024-12-31"] = {"start": "2024-10-01", "end": "2024-12-31", "val": 1, "derived": False}
        assert sec._ttm(q, "2026-03-01", 200) is None       # a gap: the four span a year

    def test_healthy_company_fields(self):
        got = sec.fundamentals_asof(_healthy(), known_by=date(2026, 3, 1), price=50.0)
        assert got["operatingMargins"] == pytest.approx(88 / 440)
        assert got["returnOnEquity"] == pytest.approx(64 / 250)
        assert got["debtToEquity"] == pytest.approx(110 / 250 * 100)   # Yahoo's percent units
        assert got["marketCap"] == pytest.approx(50_000)
        assert got["periodEnd"] == "2025-12-31"
        assert got["negativeEquity"] is False

    def test_before_the_10k_the_figures_end_at_the_third_quarter(self):
        got = sec.fundamentals_asof(_healthy(), known_by=date(2026, 1, 15), price=50.0)
        assert got["periodEnd"] == "2025-09-30"
        assert got["returnOnEquity"] == pytest.approx((15 + 16 * 3) / 200)
        assert got["debtToEquity"] is None        # no debt filed at 2025-09-30
        assert got["marketCap"] is None           # no cover-page share count filed yet

    def test_negative_equity_is_flagged_not_divided(self):
        company = _healthy(StockholdersEquity=[_inst("2025-12-31", -40, "2026-02-15", form="10-K")])
        got = sec.fundamentals_asof(company, known_by=date(2026, 3, 1))
        assert got["negativeEquity"] is True
        assert got["returnOnEquity"] is None and got["debtToEquity"] is None

    def test_operating_margin_falls_back_to_pretax_income(self):
        pretax = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
        facts = dict(_healthy().facts)
        facts.pop("OperatingIncomeLoss")
        facts[pretax] = _year(2025, 30, 30, 30, 120)
        got = sec.fundamentals_asof(_company(**facts), known_by=date(2026, 3, 1))
        assert got["operatingMargins"] == pytest.approx(120 / 440)

    def test_margin_needs_both_sums_over_the_same_quarters(self):
        facts = dict(_healthy().facts)
        facts["OperatingIncomeLoss"] = _year(2024, 20, 20, 20, 80)   # stops a year earlier
        facts["StockholdersEquity"] = [_inst("2025-03-31", 180, "2025-05-01")]
        # Revenue runs to 2025-03-31, operating income only to 2024-12-31.
        got = sec.fundamentals_asof(_company(**facts), known_by=date(2025, 6, 1))
        assert got["operatingMargins"] is None
        assert got["returnOnEquity"] is not None      # the other fields still stand

    def test_no_facts_gives_all_none(self):
        got = sec.fundamentals_asof(sec.Company("X", 1, [], None), known_by=date(2026, 3, 1), price=10.0)
        assert all(got[f] is None for f in sec.QUALITY_FIELDS)


class TestDebt:
    E, KB = "2025-12-31", "2026-03-01"

    def _debt(self, **values):
        facts = {c: [_inst(self.E, v, "2026-02-15", form="10-K")] for c, v in values.items()}
        return sec._debt_at(facts, self.E, self.KB)

    def test_debt_current_already_includes_short_term_borrowings(self):
        assert self._debt(LongTermDebtNoncurrent=100, DebtCurrent=30, ShortTermBorrowings=20) == 130

    def test_current_maturities_plus_short_term_borrowings(self):
        assert self._debt(LongTermDebtNoncurrent=100, LongTermDebtCurrent=10,
                          ShortTermBorrowings=20) == 130

    def test_commercial_paper_is_not_added_on_top_of_borrowings(self):
        assert self._debt(LongTermDebt=100, ShortTermBorrowings=20, CommercialPaper=15) == 120
        assert self._debt(LongTermDebt=100, CommercialPaper=15) == 115

    def test_combined_amount_and_short_term_only(self):
        assert self._debt(DebtLongtermAndShorttermCombinedAmount=77) == 77
        assert self._debt(DebtCurrent=12) == 12
        assert self._debt(CommercialPaper=5) == 5

    def test_no_debt_tag_at_all_is_unknown_not_zero(self):
        assert self._debt() is None

    def test_a_value_at_another_date_does_not_count(self):
        facts = {"LongTermDebt": [_inst("2025-09-30", 100, "2025-11-01")]}
        assert sec._debt_at(facts, self.E, self.KB) is None


class TestShares:
    def test_share_classes_on_one_cover_page_are_summed(self):
        facts = {"EntityCommonStockSharesOutstanding": [
            _inst("2026-01-20", 5800, "2026-02-01", form="10-K", accn="a1"),
            _inst("2026-01-20", 860, "2026-02-01", form="10-K", accn="a1"),
            _inst("2025-10-20", 6000, "2025-11-01", accn="a0"),
        ]}
        assert sec._shares(facts, "2026-03-01", 200) == 6660

    def test_diluted_weighted_shares_are_the_fallback(self):
        facts = {"WeightedAverageNumberOfDilutedSharesOutstanding": _year(2025, 10, 11, 12, 11)}
        assert sec._shares(facts, "2026-03-01", 200) == 12   # latest quarter, not a sum


class TestFilings:
    def test_real_submissions_file_parses(self):
        main = _load("submissions-AAPL.json")
        events = sec.parse_8k([main["filings"]["recent"]])
        # The 8-K/A amends an officer-change filing; amendments count.
        assert events == [{"date": "2026-09-01", "items": ["5.02"]},
                          {"date": "2026-07-30", "items": ["2.02", "9.01"]}]

    def test_flags_respect_windows_and_report_each_item_once(self):
        filings = [
            {"date": "2026-10-20", "items": ["5.02"]},
            {"date": "2026-10-01", "items": ["5.02", "9.01"]},
            {"date": "2026-08-15", "items": ["4.02"]},
            {"date": "2026-06-01", "items": ["1.03"]},       # beyond 90 days
            {"date": "2026-09-01", "items": ["2.06"]},       # beyond 30 days
        ]
        hard, soft = sec.filing_flags(filings, date(2026, 10, 23), config)
        assert hard == [{"item": "4.02", "date": "2026-08-15"}]
        assert soft == [{"item": "5.02", "date": "2026-10-20"}]

    def test_describe_is_bilingual(self):
        line = sec.describe({"item": "4.02", "date": "2026-08-15"})
        assert line.startswith("דיווח 8-K מתאריך 2026-08-15: ")
        assert line.endswith("8-K filed 2026-08-15: past financials no longer reliable (item 4.02)")
        # No price-trap marker, or score.py would weigh it as one.
        assert not any(m in line for m in ("צניחה", "שפל", "מגמת"))

    def test_older_pages_are_fetched_only_as_far_back_as_needed(self, monkeypatch):
        monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
        main = {"filings": {
            "recent": {"form": ["8-K"], "filingDate": ["2026-07-30"], "items": ["2.02"]},
            "files": [{"name": "CIK0000000001-submissions-001.json", "filingTo": "2025-12-31"},
                      {"name": "CIK0000000001-submissions-002.json", "filingTo": "2019-12-31"}],
        }}
        page = {"form": ["8-K", "4"], "filingDate": ["2024-03-01", "2024-03-02"], "items": ["4.02", ""]}
        asked = []

        def fake(url):
            asked.append(url)
            return page if url.endswith("-001.json") else main
        monkeypatch.setattr(sec, "_get_json", fake)
        events = sec.fetch_filings(1, since=date(2021, 1, 1))
        assert [e["date"] for e in events] == ["2026-07-30", "2024-03-01"]
        assert not any(u.endswith("-002.json") for u in asked)
        assert sec.fetch_filings(1) == [{"date": "2026-07-30", "items": ["2.02"]}]


class TestHttp:
    def test_without_a_user_agent_nothing_is_requested(self, monkeypatch):
        monkeypatch.setattr(sec.requests, "get", lambda *a, **k: pytest.fail("network used"))
        assert sec.enabled() is False
        assert sec.load_company("AAPL") is None

    def test_a_403_switches_sec_off_for_the_run(self, monkeypatch):
        monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
        monkeypatch.setattr(sec, "REQUEST_PAUSE", 0.0)
        calls = []

        class Resp:
            status_code = 403

        monkeypatch.setattr(sec.requests, "get", lambda *a, **k: calls.append(1) or Resp())
        assert sec._get_json(sec.TICKERS_URL) is None
        assert sec.enabled() is False
        assert sec._get_json(sec.TICKERS_URL) is None
        assert len(calls) == 1

    def test_ticker_map_uses_dashes(self):
        raw = {"0": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire"},
               "1": {"cik_str": 320193, "ticker": "aapl", "title": "Apple"}}
        m = sec.parse_ticker_map(raw)
        assert m == {"BRK-B": 1067983, "AAPL": 320193}
        assert sec.parse_ticker_map(None) == {}


class TestYahooOverlay:
    """data._apply_sec: SEC replaces the Gate 1 fields it can compute; Yahoo keeps the rest."""

    def _info(self):
        return {"returnOnEquity": 0.30, "operatingMargins": 0.25, "debtToEquity": 40.0,
                "marketCap": 9e11, "shortName": "Test"}

    def test_fields_sec_lacks_keep_yahoo_values(self, monkeypatch):
        monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
        company = _healthy()
        company.facts.pop("LongTermDebtNoncurrent")
        company.facts.pop("LongTermDebtCurrent")
        company.filings = [{"date": "2026-10-01", "items": ["5.02"]}]
        monkeypatch.setattr(sec, "load_company", lambda t, include_facts=True: company)
        monkeypatch.setattr(data, "_today", lambda: date(2026, 3, 1))

        got = data._apply_sec("TST", self._info(), price=50.0)
        assert got["returnOnEquity"] == pytest.approx(64 / 250)
        assert got["debtToEquity"] == 40.0
        assert got["qualitySource"] == {"returnOnEquity": "sec", "operatingMargins": "sec",
                                        "debtToEquity": "yahoo", "marketCap": "sec"}
        assert got["secFilings"] == company.filings

    def test_sec_failure_leaves_yahoo_untouched(self, monkeypatch):
        monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")

        def boom(*a, **k):
            raise RuntimeError("SEC down")
        monkeypatch.setattr(sec, "load_company", boom)
        assert data._apply_sec("TST", self._info(), price=50.0) == self._info()

    def test_yahoo_quality_source_only_adds_filings(self, monkeypatch):
        monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
        monkeypatch.setattr(data.config, "QUALITY_SOURCE", "yahoo")
        filings = [{"date": "2026-10-01", "items": ["4.02"]}]
        monkeypatch.setattr(sec, "load_company",
                            lambda t, include_facts=True: sec.Company(t, 1, filings, None))
        got = data._apply_sec("TST", self._info(), price=50.0)
        assert got["returnOnEquity"] == 0.30 and "qualitySource" not in got
        assert got["secFilings"] == filings

    def test_negative_equity_overrides_yahoo(self, monkeypatch):
        """SEC knowing equity is negative must not be papered over by Yahoo's ROE."""
        monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
        company = _healthy(StockholdersEquity=[_inst("2025-12-31", -40, "2026-02-15", form="10-K")])
        monkeypatch.setattr(sec, "load_company", lambda t, include_facts=True: company)
        monkeypatch.setattr(data, "_today", lambda: date(2026, 3, 1))
        got = data._apply_sec("TST", self._info(), price=50.0)
        assert got["negativeEquity"] is True
