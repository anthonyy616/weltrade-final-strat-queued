"""Offline test of the phase-E bulk open/close integration with a stubbed
MetaTrader5 module and a fake bridge. No MT5 required (runs on macOS)."""
import sys
import types
import asyncio

# --- Stub MetaTrader5 before any core import ---
mt5 = types.ModuleType("MetaTrader5")
mt5.ORDER_TYPE_BUY = 0
mt5.ORDER_TYPE_SELL = 1
mt5.ORDER_FILLING_FOK = 0
mt5.TRADE_ACTION_DEAL = 1
mt5.TRADE_RETCODE_DONE = 10009
mt5.symbol_info = lambda s: None
mt5.symbol_info_tick = lambda s: None
mt5.positions_get = lambda **kw: []
mt5.positions_total = lambda: 0
mt5.order_calc_margin = lambda *a, **k: 0.0
mt5.account_info = lambda: None
sys.modules["MetaTrader5"] = mt5

sys.path.insert(0, ".")
from core import bulk_orders as bo

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)

# --- Fake bridge ---
class FakeBridge:
    def __init__(self, healthy=True, fail_tags=None, fill_tags=None):
        self.healthy_flag = healthy
        self.fail_tags = fail_tags or set()     # never fill these, ever
        self.fill_tags = fill_tags or set()     # fill ONLY these (injected failure)
        self.open_calls = []
        self.close_all_calls = 0
        self.book = {}   # tag -> (ticket, entry)

    async def healthy(self, timeout_s=1.0):
        return self.healthy_flag

    async def open_batch(self, symbol, magic, orders):
        self.open_calls.append([o["tag"] for o in orders])
        for o in orders:
            if o["tag"] in self.fill_tags:
                self.book[o["tag"]] = (1000 + len(self.book), 1.2345)
                self.fill_tags.discard(o["tag"])   # fill once, then sticks in book
        return {"sent_ok": len(orders)}

    async def close_all(self, symbol, magic):
        self.close_all_calls += 1
        self.book.clear()
        return {}

    async def close_tickets(self, symbol, magic, tickets):
        return {}


def attach_book(br):
    """Make the mt5 stub's positions_get return the bridge's book —
    open_position_batch reconciles against positions_get, not the reply."""
    class P:
        pass
    def _pg(**kw):
        return [P() for _ in ()] if not br.book else [
            type("Pos", (), {"comment": t, "magic": bo.MAGIC_NUMBER,
                             "ticket": tk, "price_open": e})
            for t, (tk, e) in br.book.items()]
    bo.mt5.positions_get = _pg

# --- Preflight total-volume rejection with suggestions ---
try:
    orders = [{"side": "B", "lot": 0.01, "tp": 0, "sl": 0, "tag": "x"}] * 1300
    # 1300 * 0.01 = 13 lots > 12 limit for FX Vol 20
    bo._check_total_volume("FX Vol 20", orders)
    check("total volume over limit rejected", False)
except ValueError as e:
    check("total volume over limit rejected", "1140" in str(e) or "95%" in str(e))
    print("   message:", e)

# --- build_order_lines: counts, uniqueness, interleave ---
lines = bo.build_order_lines("FX Vol 20", "buy", 0.01, 0.01, 55, 55,
                             lambda u, d, n, f, s: u + n * f,
                             lambda u, d, n, f, s: d - n * f,
                             1000.0, 900.0, 10.0)
check("55+55 -> 110 lines", len(lines) == 110)
check("tags unique", len(set(o["tag"] for o in lines)) == 110)
check("tags <= 31 chars", all(len(o["tag"]) <= 31 for o in lines))
check("moving carry tp, constant do not",
      all(o["tp"] > 0 for o in lines if o["kind"] == "moving")
      and all(o["tp"] == 0 and o["sl"] == 0 for o in lines if o["kind"] == "constant"))

# --- open_position_batch: full success ---
# (preflight already exercised above; bypass the broker-info part here since
# the stub has no tick/account data)
bo.preflight = lambda symbol, orders: bo._check_total_volume(symbol, orders)

br = FakeBridge(healthy=True, fill_tags=set(o["tag"] for o in lines))
orders = bo.build_order_lines("FX Vol 20", "buy", 0.01, 0.01, 4, 4,
                              lambda u, d, n, f, s: u, lambda u, d, n, f, s: d,
                              1000.0, 900.0, 10.0)
br.fill_tags = set(o["tag"] for o in orders)
attach_book(br)
res = asyncio.run(bo.open_position_batch(br, "FX Vol 20", orders))
check("full batch returns ticket map", len(res) == len(orders))

# --- open_position_batch: partial fill -> retries -> abort, zero left ---
br2 = FakeBridge(healthy=True)
br2.fill_tags = set(list(br.fill_tags)[:len(orders) - 1])   # one line never fills
br2.book = {t: (i, 1.0) for i, t in enumerate(sorted(br2.fill_tags))}
attach_book(br2)
try:
    asyncio.run(bo.open_position_batch(br2, "FX Vol 20", orders))
    check("short batch raises BulkOpenAborted", False)
except bo.BulkOpenAborted as e:
    check("short batch raises BulkOpenAborted", True)
check("abort retried twice (3 open calls)", len(br2.open_calls) == 3)
check("abort closed everything (close_all called)", br2.close_all_calls >= 1)
check("book verified empty after abort", len(br2.book) == 0)

# --- open_position_batch: unhealthy EA -> sequential fallback signal ---
br3 = FakeBridge(healthy=False)
try:
    asyncio.run(bo.open_position_batch(br3, "FX Vol 20", orders))
    check("unhealthy EA raises UseSequentialFallback", False)
except bo.UseSequentialFallback:
    check("unhealthy EA raises UseSequentialFallback", True)

# --- late fill between passes: retries only truly-absent tags (no dup) ---
br4 = FakeBridge(healthy=True)
br4.fill_tags = set()   # first pass: nothing fills
async def run_with_late_fill():
    orig_open = br4.open_batch
    calls = {"n": 0}
    async def open_batch(symbol, magic, ords):
        calls["n"] += 1
        if calls["n"] >= 2:   # from retry pass 1 on, everything fills
            for o in ords:
                br4.book[o["tag"]] = (2000 + len(br4.book), 1.0)
        return await orig_open(symbol, magic, ords)
    br4.open_batch = open_batch
    attach_book(br4)
    return await bo.open_position_batch(br4, "FX Vol 20", orders)
res = asyncio.run(run_with_late_fill())
check("late fill recovered without duplicates", len(res) == len(orders))

print()
fails = [n for n, ok in PASS if not ok]
print(f"{len(PASS) - len(fails)}/{len(PASS)} checks passed")
sys.exit(1 if fails else 0)
