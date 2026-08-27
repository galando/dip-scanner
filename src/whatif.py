"""Value a past simulation's positions at a later date — the "what if I'd held?" question.

The monthly simulation closes its book on the last day, which answers "how did
the strategy do over its month" but not "was selling the right call". This
module re-prices the positions that were open on a given date using the cached
daily bars, so the exit rules can be judged against simply doing nothing.

Prices come from the same offline cache the replay uses. Bars are the broker's,
which applies corporate actions retroactively: for a ticker that had a
distribution the cached entry-date close is below the price the simulation
recorded live, and the difference is the distribution. Both figures are
reported — `price_pct` against the entry the bot actually paid, and
`total_return_pct` on the adjusted series, which includes it.

Usage:
    python -m src.whatif 2026-06-29            # value that day's open book today
    python -m src.whatif 2026-06-29 2026-07-15
"""
import sys
from datetime import date

import config
import src.pricecache as pricecache
import src.simulate as simulate


def positions_open_on(state: dict, as_of: date) -> list[dict]:
    """Positions the book held at the close of `as_of`.

    A finished simulation has moved everything into `closed`, so reconstruct
    from the entry/exit dates: held means entered on or before `as_of` and not
    yet sold.
    """
    day = as_of.isoformat()
    held = [p for p in state.get("positions", []) if p["entry_date"] <= day]
    held += [
        c for c in state.get("closed", [])
        if c["entry_date"] <= day < c["exit_date"]
    ]
    return sorted(held, key=lambda p: p["entry_date"])


def hold_forward(state: dict, as_of: date, until: date = None, cfg=config,
                 cache_dir: str = pricecache.CACHE_DIR) -> dict:
    """Mark the book open on `as_of` at `until`'s close, as if nothing was sold."""
    until = until or date.fromisoformat(pricecache.load_dates(cache_dir)[-1])
    slot_size = float(state.get("cash_per_stock", cfg.SIM_CASH_PER_STOCK))
    rows, cost, value = [], 0.0, 0.0
    # A position with no cached prices cannot be valued, and dropping it quietly
    # would leave the basket total reading as the whole book while describing a
    # subset of it. It is reported alongside the total instead.
    unpriced: list[str] = []
    for pos in positions_open_on(state, as_of):
        frame = pricecache.load_frame(pos["ticker"], cache_dir)
        if frame is None:
            unpriced.append(pos["ticker"])
            continue
        window = frame[frame.index <= str(until)]
        entry_window = frame[frame.index <= pos["entry_date"]]
        if window.empty or entry_window.empty:
            unpriced.append(pos["ticker"])
            continue
        price = float(window["Close"].iloc[-1])
        adj_entry = float(entry_window["Close"].iloc[-1])
        # The run's own slot size, not the live config: a row written before
        # cost_basis was recorded still belongs to the book that paid for it,
        # and charging it today's SIM_CASH_PER_STOCK would rescale a past result.
        basis = pos.get("cost_basis", slot_size)
        rows.append({
            "ticker": pos["ticker"],
            "entry_date": pos["entry_date"],
            "entry_price": pos["entry_price"],
            "current_price": price,
            "price_pct": (price / pos["entry_price"] - 1.0) * 100.0,
            "total_return_pct": (price / adj_entry - 1.0) * 100.0,
            "pnl": pos["shares"] * price - basis,
        })
        cost += basis
        value += pos["shares"] * price
    rows.sort(key=lambda r: r["price_pct"], reverse=True)
    return {
        "as_of": as_of.isoformat(),
        "until": until.isoformat(),
        "rows": rows,
        "unpriced": unpriced,
        "total_cost": cost,
        "total_value": value,
        "pnl": value - cost,
        "pnl_pct": (value / cost - 1.0) * 100.0 if cost else 0.0,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python -m src.whatif AS_OF [UNTIL]\n"
            "  AS_OF  the day whose open book to value (e.g. 2026-06-29)\n"
            "  UNTIL  the day to value it at (default: the last cached session)"
        )
    state = simulate.load_sim()
    if state is None:
        raise SystemExit(
            f"no simulation state at {config.SIM_STATE_PATH} — nothing to value. "
            f"This reads the book a past run held; run the simulation first."
        )
    as_of = date.fromisoformat(sys.argv[1])
    until = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    result = hold_forward(state, as_of, until)
    print(f"Positions open on {result['as_of']}, held to {result['until']}:\n")
    for r in result["rows"]:
        extra = ""
        if abs(r["total_return_pct"] - r["price_pct"]) > 0.1:
            extra = f"   (with distributions: {r['total_return_pct']:+.1f}%)"
        print(f"  {r['ticker']:6} ${r['entry_price']:8.2f} -> ${r['current_price']:8.2f}  "
              f"{r['price_pct']:+6.1f}%  ${r['pnl']:+8.2f}{extra}")
    print(f"\n  Basket: ${result['total_cost']:.0f} -> ${result['total_value']:.0f}  "
          f"${result['pnl']:+.0f} ({result['pnl_pct']:+.1f}%)")
    if result["unpriced"]:
        print(f"\n  NOT INCLUDED — no cached prices for "
              f"{len(result['unpriced'])} of the book: "
              + ", ".join(result["unpriced"]))
