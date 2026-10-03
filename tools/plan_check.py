"""Offline plan self-check for the limit-trigger open mode (plan phase 4).

Builds a limit plan for each symbol and prints the levels and a summary of both
scenarios so the arithmetic can be checked by hand BEFORE anything reaches the
broker.

    python3 tools/plan_check.py                       # live ticks, every symbol
    python3 tools/plan_check.py --symbol "FX Vol 20"
    python3 tools/plan_check.py --mid 1000 --entry-offset 10
    python3 tools/plan_check.py --worked-example       # doc 08 section 8

With no MT5 terminal available the tool still runs: it stubs MetaTrader5 so the
pure closing-target formulas can be imported, uses --mid instead of a live tick,
and says clearly that the stops/spread floor could not be computed.

Never sends an order. Never places a pending.
"""
import argparse
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _stub_mt5():
    """Install a minimal MetaTrader5 stub so the pure math imports offline.

    Only the closing-target formulas are needed and they touch no MT5 API;
    everything that actually talks to the broker is skipped when MT5 is absent.
    """
    try:
        import MetaTrader5  # noqa: F401
        return True
    except ImportError:
        pass
    m = types.ModuleType("MetaTrader5")

    class _Nothing:
        point = 0.01
        digits = 2
        trade_stops_level = 0
        trade_freeze_level = 0

        def __getattr__(self, _):
            return 0

    m.symbol_info = lambda *a, **k: None
    m.symbol_info_tick = lambda *a, **k: None
    m.account_info = lambda: None
    m.orders_get = lambda *a, **k: ()
    m.positions_get = lambda *a, **k: ()
    m.order_send = lambda *a, **k: None
    m.order_calc_margin = lambda *a, **k: 0.0
    m.last_error = lambda: 0
    for k in ("ORDER_TYPE_BUY", "ORDER_TYPE_SELL", "TRADE_ACTION_DEAL",
              "ORDER_TIME_GTC", "ORDER_FILLING_FOK", "DEAL_ENTRY_OUT",
              "TRADE_RETCODE_DONE", "TRADE_ACTION_REMOVE"):
        setattr(m, k, 0)
    sys.modules["MetaTrader5"] = m
    return False


def _stub_aiosqlite():
    try:
        import aiosqlite  # noqa: F401
        return True
    except ImportError:
        pass
    m = types.ModuleType("aiosqlite")
    m.Connection = object
    m.connect = lambda *a, **k: None
    sys.modules["aiosqlite"] = m
    return False


MT5_REAL = _stub_mt5()
_stub_aiosqlite()

from core import bulk_orders                                    # noqa: E402
from core.engine.queued_close_strategy_engine import (            # noqa: E402
    moving_tp_level, moving_sl_level)

MOVING_SIDE = "buy"
GRID = 50.0
MOVING_FREQ = 10.0
MOVING_TOTAL = 2
CONSTANT_TOTAL = 2
LOT = 0.01


def _fmt(tp, sl):
    if tp == 0.0 and sl == 0.0:
        return "no TP/SL"
    return f"TP {tp:.2f} / SL {sl:.2f}"


def show_plan(plan, levels, symbol, title):
    print(f"\n{'=' * 74}\n{title}  [{symbol}]\n{'=' * 74}")
    if levels:
        print(f"  requested entry_offset : {levels['requested_offset']:.2f}")
        print(f"  floor (min stop/spread): {levels['floor']:.2f}   "
              f"(min_stop_pips={levels['min_stop_pips']}, "
              f"stops_level={levels['stops_level']}, spread={levels['spread']:.2f})")
        print(f"  effective offset       : {levels['effective_offset']:.2f}"
              f"{'   <-- CLAMPED UP' if levels['clamped'] else ''}")
    print(f"  mid                    : {plan['mid']:.2f}")
    print(f"  lower level (buys)     : {plan['lower']:.2f}")
    print(f"  upper level (sells)    : {plan['upper']:.2f}")
    print(f"  moving side            : {plan['moving_side'].upper()}")
    print(f"  total lines            : {len(plan['lines'])}")

    for name, center, roles in (
            ("LOWER scenario", plan["lower"], ("PB", "CS")),
            ("UPPER scenario", plan["upper"], ("PS", "CB")),
    ):
        print(f"\n  --- {name} (center {center:.2f}) ---")
        for role in roles:
            rows = [l for l in plan["lines"] if l["role"] == role]
            if not rows:
                continue
            total_vol = sum(r["lot"] for r in rows)
            kind = rows[0]["kind"]
            print(f"    {role}: {len(rows)} order(s), side "
                  f"{rows[0]['side']}, {kind}, total vol {total_vol:.2f}")
            for r in rows[:3]:
                print(f"       slot {r['slot']} lot {r['lot']:.2f} "
                      f"tag {r['tag']:<12} {_fmt(r['tp'], r['sl'])}")
            if len(rows) > 3:
                print(f"       ... and {len(rows) - 3} more")
    return plan


def build(symbol, mid, entry_offset, live):
    if live:
        import MetaTrader5 as mt5
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise SystemExit(f"no symbol info/tick for {symbol} on this terminal")
        mid = (tick.ask + tick.bid) / 2
        levels = bulk_orders.compute_limit_levels(symbol, mid, entry_offset,
                                                  info, tick)
    else:
        levels = None
        lower, upper = mid - entry_offset, mid + entry_offset
        levels = {"requested_offset": entry_offset,
                  "effective_offset": entry_offset,
                  "floor": 0.0, "clamped": False, "spread": 0.0,
                  "min_stop_pips": 0, "stops_level": 0, "point": 0.0,
                  "digits": 2, "mid": mid, "lower": round(lower, 2),
                  "upper": round(upper, 2)}

    plan = bulk_orders.build_limit_plan(
        symbol, MOVING_SIDE, LOT, LOT, MOVING_TOTAL, CONSTANT_TOTAL,
        moving_tp_level, moving_sl_level, GRID, MOVING_FREQ,
        levels["lower"], levels["upper"])
    plan["mid"] = mid
    return plan, levels


def worked_example():
    """Reproduce doc 08 section 8 and assert it matches the documented result."""
    print("Worked example from doc 08 section 8: moving=BUY, mid 1000, offset 10")
    print("  Lower scenario (center 990): PB buys at 990 with moving-slot TP/SL\n"
          "  anchored on 990; CS constant sells at market, no TP/SL.\n"
          "  Upper scenario (center 1010): PS constant sells at 1010, no TP/SL;\n"
          "  CB moving buys at market with moving-slot TP/SL anchored on 1010.\n")
    plan, _ = build("WORKED", 1000.0, 10.0, live=False)
    plan.update({"lower": 990.0, "upper": 1010.0})

    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  ({detail})" if detail else ""))
        ok = ok and cond

    pb = [l for l in plan["lines"] if l["role"] == "PB"]
    cs = [l for l in plan["lines"] if l["role"] == "CS"]
    ps = [l for l in plan["lines"] if l["role"] == "PS"]
    cb = [l for l in plan["lines"] if l["role"] == "CB"]

    check("2 PB lines (2 moving slots, lot under the split cap)", len(pb) == 2,
          str(len(pb)))
    check("PB are buys at the lower level", all(l["side"] == "B" for l in pb))
    check("PB carry moving TP/SL", all(l["tp"] != 0.0 and l["sl"] != 0.0 for l in pb))
    # center 990 -> up 1040, down 940; moving=BUY -> TP = up + n*10, SL = down - n*10
    check("PB slot 1 TP/SL anchored on 990",
          (pb[0]["tp"], pb[0]["sl"]) == (1050.0, 930.0),
          f"tp={pb[0]['tp']} sl={pb[0]['sl']}")
    check("PB slot 2 TP/SL anchored on 990",
          (pb[1]["tp"], pb[1]["sl"]) == (1060.0, 920.0),
          f"tp={pb[1]['tp']} sl={pb[1]['sl']}")
    check("CS are sells with NO TP/SL",
          all(l["side"] == "S" and l["tp"] == 0.0 and l["sl"] == 0.0 for l in cs))
    check("PS are sells at the upper level with NO TP/SL",
          all(l["side"] == "S" and l["tp"] == 0.0 and l["sl"] == 0.0 for l in ps))
    # center 1010 -> up 1060, down 960; CB buys are the moving side here
    check("CB slot 1 TP/SL anchored on 1010",
          (cb[0]["tp"], cb[0]["sl"]) == (1070.0, 950.0),
          f"tp={cb[0]['tp']} sl={cb[0]['sl']}")
    check("CB are buys", all(l["side"] == "B" for l in cb))
    check("exactly one of PB/CS carries stops",
          (pb[0]["tp"] != 0.0) ^ (cs[0]["tp"] != 0.0))
    check("exactly one of PS/CB carries stops",
          (ps[0]["tp"] != 0.0) ^ (cb[0]["tp"] != 0.0))
    check("all tags unique", len({l["tag"] for l in plan["lines"]}) == len(plan["lines"]))
    check("all tags <= 31 chars",
          all(len(l["tag"]) <= 31 for l in plan["lines"]))
    show_plan(plan, None, "WORKED", "Plan for the worked example")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", action="append")
    ap.add_argument("--mid", type=float,
                    help="synthetic mid price (implies offline mode)")
    ap.add_argument("--entry-offset", type=float, default=50.0)
    ap.add_argument("--worked-example", action="store_true")
    args = ap.parse_args()

    if args.worked_example:
        return worked_example()

    live = MT5_REAL and args.mid is None
    if not MT5_REAL:
        print("NOTE: MetaTrader5 is not importable here, running OFFLINE.")
        print("      The stops/spread floor is NOT applied (it needs a live "
              "symbol_info). Pass --mid to pick the centre price.")
        print("      Run this on the bot machine with the terminal up to see "
              "the real floor and preflight results.\n")
    elif not live:
        print(f"OFFLINE mode, mid forced to {args.mid}\n")

    if live:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            print("mt5.initialize() failed — is the terminal running?")
            return 1

    symbols = args.symbol or [
        "FX Vol 20", "FX Vol 40", "FX Vol 60", "FX Vol 80", "FX Vol 99",
        "SFX Vol 20", "SFX Vol 40", "SFX Vol 60", "SFX Vol 80", "SFX Vol 99",
    ]
    failures = 0
    for sym in symbols:
        try:
            plan, levels = build(sym, args.mid or 0.0, args.entry_offset, live)
        except SystemExit as e:
            print(f"{sym}: {e}")
            continue
        except Exception as e:
            print(f"{sym}: plan build FAILED: {type(e).__name__}: {e}")
            failures += 1
            continue
        show_plan(plan, levels, sym, "Limit plan")
        if live:
            try:
                bulk_orders.preflight_limit(sym, plan, levels)
                print("  preflight            : PASS")
            except bulk_orders.LimitPreflightError as e:
                print(f"  preflight            : FAIL — {e}")
                failures += 1

    print(f"\n{'=' * 74}")
    print("Nothing was sent to the broker. This tool only prints arithmetic.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())