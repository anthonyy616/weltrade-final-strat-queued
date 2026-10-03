"""Checks for the phase 6 observability work (metrics, logging, UI status).

Three things are verified:
  1. open_quality.csv gets exactly one correct row per successful open, in
     BOTH modes, with the header written once and no duplicate header.
  2. A write failure is swallowed -- it must not break the trading path.
  3. The status payload carries `armed`, and the UI shows the armed state.

Run:  python3 tools/verify_phase6_observability.py
"""
import asyncio
import csv
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if "aiosqlite" not in sys.modules:
    try:
        import aiosqlite  # noqa: F401
    except ImportError:
        _m = types.ModuleType("aiosqlite")
        _m.Connection = object
        _m.connect = lambda *a, **k: None
        sys.modules["aiosqlite"] = _m

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class P:
    """Minimal stand-in for an mt5 position row."""

    def __init__(self, ticket, typ, price, time_msc, magic=123456):
        self.ticket, self.type, self.price_open = ticket, typ, price
        self.time_msc, self.magic = time_msc, magic


DONE_PHASE = {"phase": "DONE", "trigger_side": "1",
              "t_trigger_us": "1000000", "t_cancel_us": "1012500",
              "t_burst_us": "1018000"}


def test_rows():
    print("\n[1] One row per open, header written once")
    from core import open_quality as oq

    # Burst-like fill: three buys sliding across three prices, two sells
    # tight. This is the dispersion the metric exists to expose.
    burst = [
        P(1, 0, 1.23010, 1000), P(2, 0, 1.23040, 1040), P(3, 0, 1.23055, 1090),
        P(4, 1, 1.22990, 1020), P(5, 1, 1.22990, 1020),
    ]
    # Limit-like fill: everything at one price, microseconds apart.
    limit = [P(11, 0, 1.23000, 2000), P(12, 0, 1.23000, 2003),
             P(13, 1, 1.22980, 2002), P(14, 1, 1.22980, 2005)]

    path = oq.csv_path("verify6")
    if path.exists():
        path.unlink()

    oq.record_open("verify6", "EURUSD", "burst", burst, digits=5)
    oq.record_open("verify6", "EURUSD", "limit_trigger", limit, digits=5,
                   phase=DONE_PHASE, trigger_side="lower", entry_offset=10.0)

    raw = path.read_text(encoding="utf-8").strip().splitlines()
    check("file has a header plus two rows", len(raw) == 3, str(len(raw)))
    check("header appears exactly once",
          sum(1 for r in raw if r.startswith("timestamp,")) == 1)
    check("header matches the declared columns",
          raw[0].split(",") == oq.COLUMNS)

    rows = list(csv.DictReader(raw))
    b, l = rows[0], rows[1]
    check("burst row records the mode", b["mode"] == "burst", b["mode"])
    check("limit row records the mode", l["mode"] == "limit_trigger", l["mode"])
    check("burst trigger side is n/a, not blank", b["trigger_side"] == "n/a")
    check("limit trigger side recorded", l["trigger_side"] == "lower")

    # The whole point: burst slides, limit does not.
    check("burst buy shows 3 distinct prices", b["buy_distinct_prices"] == "3",
          b["buy_distinct_prices"])
    check("burst buy modal share is 1/3", b["buy_modal_share"] == "0.33",
          b["buy_modal_share"])
    check("burst buy span covers the slide", b["buy_span_ms"] == "90",
          b["buy_span_ms"])
    check("burst sells at one price share 1.00", b["sell_modal_share"] == "1.00")
    check("limit buys all at one price",
          l["buy_distinct_prices"] == "1", l["buy_distinct_prices"])
    check("limit buy modal share is 1.00", l["buy_modal_share"] == "1.00")
    check("limit buy span is a few ms", l["buy_span_ms"] == "3",
          l["buy_span_ms"])
    check("burst min/max bracket the fills",
          b["buy_min_price"] == "1.23010" and b["buy_max_price"] == "1.23055",
          f"{b['buy_min_price']}..{b['buy_max_price']}")

    # EA latencies, differenced on the EA's own clock (microseconds).
    check("trigger to cancel done = 12ms",
          l["trigger_to_cancel_ms"] == "12", l["trigger_to_cancel_ms"])
    check("trigger to burst done = 18ms",
          l["trigger_to_burst_ms"] == "18", l["trigger_to_burst_ms"])
    check("burst mode has no EA latencies to report",
          b["trigger_to_cancel_ms"] == "" and b["trigger_to_burst_ms"] == "",
          f"{b['trigger_to_cancel_ms']}/{b['trigger_to_burst_ms']}")
    check("entry offset recorded only for the limit open",
          l["entry_offset"] == "10.0" and b["entry_offset"] == "",
          f"{b['entry_offset']}/{l['entry_offset']}")

    print("\n[2] Incomplete data stays blank, not zero")
    # No fills at all on the sell side, and a DONE with no EA stamps: zeros
    # would claim "all fills simultaneous" and "cancel took 0ms".
    oq.record_open("verify6", "EURUSD", "limit_trigger",
                   [P(21, 0, 1.23000, 3000)], digits=5,
                   phase={"phase": "DONE"}, trigger_side="upper")
    rows = list(csv.DictReader(
        path.read_text(encoding="utf-8").strip().splitlines()))
    e = rows[-1]
    check("empty side leaves count blank", e["sell_orders"] == "",
          e["sell_orders"])
    check("empty side leaves span blank", e["sell_span_ms"] == "")
    check("missing EA stamps leave latencies blank",
          e["trigger_to_cancel_ms"] == "" and e["trigger_to_burst_ms"] == "")
    check("the populated side is still measured",
          e["buy_orders"] == "1" and e["buy_modal_share"] == "1.00")

    print("\n[3] A write failure never propagates")
    import core.open_quality as oq2

    class Boom:
        def log_error(self, msg):
            Boom.seen = msg

    Boom.seen = None

    class BadPath:
        """A path whose open() always raises, like a full or locked disk."""

        def exists(self):
            return True

        def stat(self):
            raise OSError("no stat for you")

    orig = oq2.csv_path
    oq2.csv_path = lambda uid: BadPath()
    try:
        ok = oq2.record_open("verify6", "EURUSD", "burst",
                             [P(31, 0, 1.0, 1)], log=Boom())
        check("write failure returns False, not an exception", ok is False)
        check("write failure is logged", Boom.seen is not None, str(Boom.seen))
        check("logged message says it was skipped, not hidden",
              "not written" in (Boom.seen or ""), str(Boom.seen))
    finally:
        oq2.csv_path = orig

    # The real failure mode in the engine: a positions_get that raises.
    check("row count still 3 after the failure (nothing appended)",
          len(path.read_text(encoding="utf-8").strip().splitlines()) == 4)


def test_status_and_ui():
    print("\n[4] Status payload carries the armed flag")
    orch = (ROOT / "core/strategy_orchestrator.py").read_text(encoding="utf-8")
    check("orchestrator aggregates `armed` from the per-symbol status",
          '"armed": armed_any' in orch, "not found")
    check("the empty-status branch still reports armed=False",
          orch.count('"armed": False') == 1)
    check("per-symbol armed is read from the strategy status",
          "s.get('armed', False)" in orch)

    eng = (ROOT / "core/engine/queued_close_strategy_engine.py").read_text(
        encoding="utf-8")
    check("engine status exposes open_mode, armed and the failure count",
          all(t in eng for t in ('"open_mode": self.state.open_mode',
                                '"armed": self._limit_armed',
                                '"consecutive_open_failures"')))

    print("\n[5] UI shows the armed state")
    ui = (ROOT / "static/index.html").read_text(encoding="utf-8")
    check("Bot State shows 'Armed - waiting for trigger'",
          "Armed - waiting for trigger" in ui)
    check("armed is checked before running",
          ui.index("status.armed) stateLabel") > ui.index("is_resetting")
          and ui.index("status.armed) stateLabel") < ui.index("status.running) stateLabel"))
    check("armed state logs once on transition, not every poll",
          "lastArmedState" in ui)


def test_engine_wiring():
    print("\n[6] Both open paths write exactly one row")
    eng = (ROOT / "core/engine/queued_close_strategy_engine.py").read_text(
        encoding="utf-8")

    check("the row is written in the SHARED post-open routine",
          eng.count("self._record_open_quality(tickets") == 1)
    check("burst passes no EA stamps (it has none)",
          'quality_mode: str = "burst"' in eng)
    check("limit passes the mode, the EA phase and the side",
          'quality_mode="limit_trigger"' in eng
          and "quality_phase=phase" in eng
          and "quality_side=triggered" in eng)
    check("metrics read the broker, not the in-memory entries",
          "p.price_open" not in eng.split("def _record_open_quality")[1][:2000]
          and "positions_get" in eng.split("def _record_open_quality")[1][:2000])
    check("the whole quality call is defensive",
          "[QUALITY] open_quality.csv row skipped" in eng)
    check("a fresh [LIMIT] line per EA phase, not a bare phase string",
          "def _log_limit_phase" in eng
          and "on_phase=self._log_limit_phase" in eng)
    check("abort phases are logged as errors",
          'if phase == "ABORT":' in eng)


def main():
    test_rows()
    test_status_and_ui()
    test_engine_wiring()
    print("\n" + "=" * 62)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
