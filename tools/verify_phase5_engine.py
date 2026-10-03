"""Functional checks for the engine's limit-trigger open path (plan phase 5).

Drives QueuedCloseStrategyEngine._try_limit_open against a stubbed broker and a
stubbed EA bridge, covering the four outcomes that matter:

  success -> shared post-open, centre set to the TRIGGERED level
  ARM_TIMEOUT -> re-arm around the new price, not a new cycle
  reconcile mismatch -> sweep, flatten, count the failure
  repeated failures -> circuit breaker stops the symbol

Also asserts the persistence shape (phase/levels/cmd id, no ticket list) and
that the armed-recovery path restarts idle.

    python3 tools/verify_phase5_engine.py
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# aiosqlite is only needed for the type in a signature we stub out below.
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


class Ticker:
    def __init__(self, ask, bid):
        self.ask, self.bid = ask, bid


class Info:
    def __init__(self, point=0.01, digits=2, stops=0, freeze=0):
        self.point, self.digits = point, digits
        self.trade_stops_level, self.trade_freeze_level = stops, freeze


class Account:
    limit_orders = 200
    margin_free = 100000.0


class Pos:
    def __init__(self, ticket, comment, price, magic=123456, typ=0):
        self.ticket, self.comment, self.price_open = ticket, comment, price
        self.magic, self.type = magic, typ


class Order:
    def __init__(self, ticket, symbol, magic):
        self.ticket, self.symbol, self.magic = ticket, symbol, magic


def install_mt5(mid=1000.0, pending=(), positions=()):
    m = types.ModuleType("MetaTrader5")
    m.TICK = object()
    for k in ("TRADE_RETCODE_DONE", "TRADE_ACTION_REMOVE", "ORDER_TYPE_BUY",
              "ORDER_TYPE_SELL", "DEAL_ENTRY_OUT", "ORDER_TIME_GTC",
              "ORDER_FILLING_FOK"):
        setattr(m, k, 0 if "TYPE" in k or "TIME" in k or "FILLING" in k else 10009)
    state = {"pending": list(pending), "positions": list(positions),
             "removed": []}
    m._state = state
    m.symbol_info = lambda s: Info()
    m.symbol_info_tick = lambda s: Ticker(mid + 0.5, mid - 0.5)
    m.account_info = lambda: Account()
    m.orders_get = lambda symbol=None: (
        list(state["pending"]) if symbol is None
        else [o for o in state["pending"] if o.symbol == symbol])
    m.positions_get = lambda symbol=None, **k: list(state["positions"])
    m.order_send = lambda req: (state["removed"].append(req.get("position")),
                                types.SimpleNamespace(retcode=10009,
                                                      comment="ok"))[1]
    m.order_calc_margin = lambda *a, **k: 0.0
    m.last_error = lambda: 0
    m.history_deals_get = lambda **k: []
    sys.modules["MetaTrader5"] = m
    return m, state


class FakeConfig:
    def __init__(self, cfg):
        self._cfg = cfg

    def get_symbol_config(self, s):
        return self._cfg

    def get_global_config(self):
        return {}

    def get_config(self):
        return {"symbols": {"X": self._cfg}}


class FakeRepo:
    def __init__(self):
        self.targets = []
        self.moving = []
        self.constants = []
        self.cleared = 0

    async def save_constant_targets(self, cycle, targets):
        self.targets.append((cycle, list(targets)))

    async def save_moving_position(self, *a, **k):
        self.moving.append(a)

    async def save_constant_ticket(self, *a, **k):
        self.constants.append(a)

    async def clear_constant_queue(self, *a, **k):
        self.cleared += 1

    async def clear_constant_tickets(self):
        self.cleared += 1

    async def clear_moving_positions(self):
        self.cleared += 1


class FakeBridge:
    """EA bridge stub. `script` is a list of phase dicts returned in order.

    The broker state is materialised from the plan the ENGINE actually built,
    so tags always match even though build_limit_plan stamps a random batch
    id into every tag. `fill=False` simulates a reconcile mismatch.
    """

    def __init__(self, script, mt5_state=None, fill="lower"):
        self.script = list(script)
        self.calls = []
        self.aborted = 0
        self._state = mt5_state
        self._fill = fill

    async def healthy(self):
        return True

    async def arm_limit(self, symbol, magic, plan, **kw):
        self.calls.append({"plan": plan, "kw": kw})
        if self._fill and self._state is not None:
            # Only the TRIGGERED half fills; the loser is cancelled by the EA.
            roles = ("PB", "CS") if self._fill == "lower" else ("PS", "CB")
            self._state["positions"] = [
                Pos(1000 + i, l["tag"], plan["lower"])
                for i, l in enumerate(plan["lines"]) if l["role"] in roles]
            self._state["pending"] = []
        # Repeat the final scripted outcome instead of exhausting the script:
        # the retry loop may arm more times than we predicted.
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    async def abort_arm(self, symbol, magic, **kw):
        self.aborted += 1
        return {"accepted": "1"}

    async def cancel_all_pendings(self, symbol, magic, **kw):
        self.calls.append({"swept": (symbol, magic)})
        return True


def make_engine(script, cfg_overrides=None, mid=1000.0, fill="lower"):
    mt5, state = install_mt5(mid=mid)
    cfg = {"enabled": True, "constant_side": "sell", "buy_count": 2,
           "sell_count": 2, "buy_lot": 0.01, "sell_lot": 0.01,
           "grid_distance": 50.0, "moving_freq": 10.0,
           "constant_freq": 8.0, "open_mode": "limit_trigger",
           "entry_offset": 10.0}
    cfg.update(cfg_overrides or {})
    import importlib
    import core.engine.queued_close_strategy_engine as eng
    importlib.reload(eng)
    e = eng.QueuedCloseStrategyEngine(FakeConfig(cfg), "X", "u")
    e.ea_bridge = FakeBridge(script, mt5_state=state, fill=fill)
    e.ea_status = {"available": True, "version": "1.3", "reason": ""}
    repo = FakeRepo()
    e.repository = repo
    async def _repo():
        return repo
    e._ensure_repository_async = _repo
    e._save_calls = []
    async def _save():
        e._save_calls.append(eng.json.loads(eng.json.dumps({
            "phase": e.state.phase, "arm_lower": e.state.arm_lower,
            "arm_upper": e.state.arm_upper,
            "arm_cmd_id": e.state.arm_cmd_id})))
    e._save_symbol_state = _save
    e._force_close_everything = lambda: asyncio.sleep(0)
    return e, repo, state, mt5


DONE_LOWER = {"phase": "DONE", "trigger_side": "1", "reason": "",
              "t_trigger_us": "100", "t_cancel_us": "200", "t_burst_us": "300"}
ARM_TIMEOUT = {"phase": "ABORT", "trigger_side": "0", "reason": "ARM_TIMEOUT"}


def prep_limit_engine(e, max_failures=3):
    """Put an engine in the state start() would have left it in."""
    e.state.moving_side = "buy"
    e.state.moving_total = 2
    e.state.constant_total = 2
    e.state.moving_lot = 0.01
    e.state.constant_lot = 0.01
    e.state.moving_freq = 10.0
    e.state.constant_freq = 8.0
    e.state.grid_distance = 50.0
    e.state.open_mode = "limit_trigger"
    e.state.entry_offset = 10.0
    e.state.armed_timeout_seconds = 120
    e.state.win_fill_deadline_ms = 1500
    e.state.cancel_ack_deadline_ms = 1500
    e.state.burst_mode = "AFTER_CANCEL"
    e.state.max_consecutive_open_failures = max_failures
    e.running = True
    e.state.cycle_count = 1
    return e


async def main():
    # ---------------------------------------------------------------- success
    print("\n[1] Happy path, lower trigger")
    e, repo, state, mt5 = make_engine([dict(DONE_LOWER)])
    prep_limit_engine(e, max_failures=3)

    # The broker state a DONE implies (every plan line filled, nothing pending)
    # is materialised by the fake bridge from the plan the engine actually
    # built, so the tags line up despite the random batch id in each tag.
    handled = await e._try_limit_open("MovingBuy", "ConstantSell")
    check("open handled", handled is True)
    plan2 = e.ea_bridge.calls[0]["plan"]
    check("failure counter reset after success",
          e._consecutive_open_failures == 0, e._consecutive_open_failures)
    check("centre is the TRIGGERED lower level, not the mid",
          e.state.center_price == plan2["lower"],
          f"{e.state.center_price} vs {plan2['lower']}")
    check("grid levels re-derived from the new centre",
          e.state.grid_level_up == plan2["lower"] + 50.0
          and e.state.grid_level_down == plan2["lower"] - 50.0)
    check("moving positions registered",
          len(e.state.moving_positions) == e.state.moving_total,
          str(len(e.state.moving_positions)))
    check("constant tickets registered",
          len(e.state.constant_tickets) == e.state.constant_total,
          str(len(e.state.constant_tickets)))
    check("constant targets persisted for the triggered centre",
          bool(repo.targets) and all(
              t["price"] is not None for _, rows in repo.targets for t in rows))
    check("ARMED cmd id cleared after success", e.state.arm_cmd_id == "")

    print("\n[2] ARMED is persisted with phase, levels and cmd id (no tickets)")
    e, repo, state, mt5 = make_engine([dict(ARM_TIMEOUT)] * 5)
    prep_limit_engine(e, max_failures=1)
    await e._try_limit_open("MovingBuy", "ConstantSell")
    armed_rows = [c for c in e._save_calls if c["phase"] == "ARMED"]
    check("an ARMED row was persisted", bool(armed_rows))
    if armed_rows:
        row = armed_rows[0]
        check("ARMED persisted both levels",
              row["arm_lower"] > 0 and row["arm_upper"] > row["arm_lower"],
              str(row))
        check("ARMED persisted the cmd id", bool(row["arm_cmd_id"]), str(row))
        check("ARMED row carries NO pending ticket list",
              not any(k in row for k in
                      ("pending_tickets", "tickets", "orders", "ticket_list")))

    print("\n[3] ARM_TIMEOUT re-arms instead of counting a failure")
    # max_failures=1 stops on the very first definitive failure, so the run is
    # exactly: 1 arm + MAX_LIMIT_REARMS re-arms, then REARM_EXHAUSTED.
    import core.engine.queued_close_strategy_engine as engmod
    e, repo, state, mt5 = make_engine([dict(ARM_TIMEOUT)] * 6)
    prep_limit_engine(e, max_failures=1)
    cycle_before = e.state.cycle_count
    arms = 0
    real_arm = e.ea_bridge.arm_limit

    async def _arm(*a, **k):
        nonlocal arms
        arms += 1
        return await real_arm(*a, **k)
    e.ea_bridge.arm_limit = _arm
    await e._try_limit_open("MovingBuy", "ConstantSell")
    check("re-armed exactly MAX_LIMIT_REARMS times before giving up",
          arms == engmod.MAX_LIMIT_REARMS + 1, str(arms))
    check("re-arms did NOT increment the cycle count",
          e.state.cycle_count == cycle_before,
          f"{cycle_before} -> {e.state.cycle_count}")
    check("3 re-arms counted as ONE failure, not four",
          e._consecutive_open_failures == 1,
          e._consecutive_open_failures)
    check("exhausted re-arms stopped the symbol (breaker limit 1)",
          e.running is False)
    check("ARMED was persisted before each re-arm", bool(
        [c for c in e._save_calls if c["phase"] == "ARMED"]))

    print("\n[4] Circuit breaker stops the symbol")
    e, repo, state, mt5 = make_engine(
        [{"phase": "ABORT", "reason": "BOTH_SIDED"}] * 10)
    prep_limit_engine(e, max_failures=2)
    await e._try_limit_open("MovingBuy", "ConstantSell")
    check("breaker tripped at the configured limit",
          e._consecutive_open_failures >= 2, str(e._consecutive_open_failures))
    check("symbol stopped running", e.running is False)
    check("phase left IDLE", e.state.phase == "IDLE", e.state.phase)

    print("\n[5] Reconcile mismatch is treated as a failure")
    # fill=False: the broker reports DONE but no position was ever created.
    e, repo, state, mt5 = make_engine([dict(DONE_LOWER)], fill=False)
    prep_limit_engine(e, max_failures=1)
    # fill=False means nothing was created, so the reconcile must find 0/4.
    await e._try_limit_open("MovingBuy", "ConstantSell")
    check("reconcile mismatch counted as a failure",
          e._consecutive_open_failures >= 1, str(e._consecutive_open_failures))
    check("reconcile mismatch did NOT register positions",
          not e.state.moving_positions)

    print("\n[6] EA unavailable -> refuses to arm, never falls back")
    e, repo, state, mt5 = make_engine([])
    e.ea_status = {"available": False, "reason": "compile failed"}
    e.state.moving_side = "buy"
    e.state.open_mode = "limit_trigger"
    e.state.entry_offset = 10.0
    e.state.armed_timeout_seconds = 120
    e.state.burst_mode = "AFTER_CANCEL"
    e.state.max_consecutive_open_failures = 1
    e.running = True
    e.state.cycle_count = 1
    await e._try_limit_open("MovingBuy", "ConstantSell")
    # A sweep call is expected (the failure path must clean up); what must NOT
    # happen is an arm attempt.
    check("no arm attempted when the EA is down",
          not any("plan" in c for c in e.ea_bridge.calls),
          str(e.ea_bridge.calls))
    check("refusal counted as an open failure",
          e._consecutive_open_failures >= 1)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))