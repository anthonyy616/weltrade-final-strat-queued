"""Open-quality metrics: how tightly did this open's ladder actually fill?

Written once per successful open in BOTH modes (burst and limit_trigger) to
logs/users/{user}/sessions/open_quality.csv, so the two modes can be compared
on real data instead of argued about (doc 08 section 8).

The question this answers is fill dispersion: if the ladder fills at eight
different prices, the cycle's target math is already off before the first
target is hit. Burst mode fires every order at market and slides as it goes;
the limit ladder waits for one price and should fill tight. The modal share
and the fill span per side are the two numbers that show the difference.

Everything here is best-effort telemetry. A write failure must never break the
trading path, so every entry point swallows its own errors.
"""

import csv
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Written once per file. Kept flat and explicit so the file stays readable in
# a spreadsheet and diffable across a session.
COLUMNS = [
    "timestamp",
    "symbol",
    "mode",
    "trigger_side",
    "buy_orders",
    "buy_distinct_prices",
    "buy_min_price",
    "buy_max_price",
    "buy_modal_price",
    "buy_modal_share",
    "buy_span_ms",
    "sell_orders",
    "sell_distinct_prices",
    "sell_min_price",
    "sell_max_price",
    "sell_modal_price",
    "sell_modal_share",
    "sell_span_ms",
    "trigger_to_cancel_ms",
    "trigger_to_burst_ms",
    "entry_offset",
]

_HEADER = ",".join(COLUMNS)


def csv_path(user_id: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "logs" / "users" / user_id / "sessions" / "open_quality.csv"


def _stats(prices: List[float], times_ms: List[int], digits: int):
    """(orders, distinct, min, max, modal, modal_share, span_ms) for one side.

    `times_ms` is the MT5 open time of each fill, so the span is measured
    across real broker fills rather than across our own send loop. An empty
    side returns blanks rather than zeroes: zero would read like "all fills
    were simultaneous", which is a claim we cannot make for no fills at all.
    """
    if not prices:
        return ("", "", "", "", "", "", "")

    # Round to the symbol's digits so 1.230004 and 1.23001 count as the same
    # price. Comparing raw doubles would report dispersion that does not exist.
    rounded = [round(p, digits) for p in prices]
    counts = Counter(rounded)
    modal_price, modal_n = counts.most_common(1)[0]
    share = modal_n / len(rounded)
    span = (max(times_ms) - min(times_ms)) if times_ms else ""
    return (len(rounded), len(counts), f"{min(rounded):.{digits}f}",
            f"{max(rounded):.{digits}f}", f"{modal_price:.{digits}f}",
            f"{share:.2f}", span)


def _ea_latencies(phase: Optional[dict]):
    """(trigger_to_cancel_ms, trigger_to_burst_ms) from the EA's DONE stamps.

    The EA reports microseconds on its own clock; we only difference them, so
    no clock sync is needed. Anything missing stays blank -- a blank means
    "the EA did not report it", which is honest, where 0 would mean
    "instantaneous".
    """
    if not phase:
        return "", ""
    try:
        t = int(phase.get("t_trigger_us") or 0)
        cancel = phase.get("t_cancel_us")
        burst = phase.get("t_burst_us")
        # 1000 us -> 1 ms
        return ((int(cancel) - t) // 1000 if cancel else "",
                (int(burst) - t) // 1000 if burst else "")
    except (TypeError, ValueError):
        return "", ""


def record_open(user_id: str, symbol: str, mode: str, positions: list,
                digits: int = 5, phase: Optional[dict] = None,
                trigger_side: str = "n/a", entry_offset: Optional[float] = None,
                log=None) -> bool:
    """Append one row describing how an open filled. Returns True if written.

    `positions` are the positions_get rows for this open, carrying price_open
    and time_msc. `phase` is the EA's terminal phase dict for limit opens
    (None in burst mode, which is why its latencies come out blank).

    `log` is an optional logger exposing log_error; a write failure is routed
    there and swallowed, because losing a metrics row must never stop trading.
    """
    def _fail(msg):
        if log is not None:
            try:
                log.log_error(f"[QUALITY] open_quality.csv not written: {msg}")
            except Exception:
                pass
        return False

    try:
        buys = [p for p in positions
                if int(getattr(p, "type", 0)) == 0]
        sells = [p for p in positions
                 if int(getattr(p, "type", 0)) != 0]

        b = _stats([p.price_open for p in buys],
                   [int(getattr(p, "time_msc", 0) or 0) for p in buys], digits)
        s = _stats([p.price_open for p in sells],
                   [int(getattr(p, "time_msc", 0) or 0) for p in sells], digits)
        cancel_ms, burst_ms = _ea_latencies(phase)

        row = [datetime.now().isoformat(timespec="milliseconds"), symbol,
               mode, trigger_side, *b, *s, cancel_ms, burst_ms,
               "" if entry_offset is None else entry_offset]

        path = csv_path(user_id)
        os.makedirs(path.parent, exist_ok=True)
        # Header once: if the file exists and already has content, append.
        # Re-writing it would double every column name.
        write_header = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(COLUMNS)
            w.writerow(row)
        return True
    except Exception as e:
        return _fail(str(e))
