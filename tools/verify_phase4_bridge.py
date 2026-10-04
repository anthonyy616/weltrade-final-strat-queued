"""Verifies the limit-trigger bridge contract (plan phase 4).

Two properties matter and neither is provable by reading:

  1. Nothing hangs. Every wait has a deadline and raises instead.
  2. Phase files are read NON-DESTRUCTIVELY, so polling never deletes the
     file the EA is still writing.

A fake EA plays the file protocol out of a temp folder, so the real command
file, result file and phase file paths are exercised. No orders are sent and no
terminal is needed.

    python3 tools/verify_phase4_bridge.py
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class FakeOrders(list):
    """Stand-in for the account's pending orders, removable via order_send."""


REMOVED = []


def _install_mt5_stub(pending):
    m = types.ModuleType("MetaTrader5")
    m.TRADE_ACTION_REMOVE = 1
    m.TRADE_RETCODE_DONE = 10009

    def orders_get(symbol=None):
        if symbol is None:
            return list(pending)
        return [o for o in pending if o.sym == symbol]

    def order_send(req):
        if req.get("action") == m.TRADE_ACTION_REMOVE:
            tkt = req.get("order")
            REMOVED.append((req.get("symbol"), tkt))
            for o in list(pending):
                if o.ticket == tkt:
                    pending.remove(o)
            return types.SimpleNamespace(retcode=m.TRADE_RETCODE_DONE,
                                         comment="removed")
        return types.SimpleNamespace(retcode=0, comment="")

    class _O:
        def __init__(self, ticket, sym, magic):
            self.ticket, self.sym, self.magic = ticket, sym, magic

    m._make = _O
    m.orders_get = orders_get
    m.order_send = order_send
    m.last_error = lambda: 0
    sys.modules["MetaTrader5"] = m
    return m


async def fake_ea(folder, rid_phases, stop_event, accept=True, reason="ARMING"):
    """Minimal EA: answer the command, then publish a scripted phase sequence."""
    cmd = Path(folder) / "wt_cmd.txt"
    res = Path(folder) / "wt_res.txt"
    while not stop_event.is_set():
        if cmd.exists():
            try:
                lines = cmd.read_text(encoding="ascii").splitlines()
            except OSError:
                await asyncio.sleep(0.005)
                continue
            rid = ""
            symbol = ""
            for ln in lines:
                if ln.startswith("id="):
                    rid = ln[3:]
                elif ln.startswith("symbol="):
                    symbol = ln[7:]
            cmd.unlink(missing_ok=True)
            res.write_text(
                f"id={rid}\naction=ARMLIMIT\nsymbol={symbol}\n"
                f"accepted={1 if accept else 0}\nreason={reason}\n"
                "version=1.3\n", encoding="ascii")
            for i, ph in enumerate(rid_phases):
                pf = Path(folder) / f"wt_arm_{rid}.txt"
                pf.write_text(
                    f"id={rid}\nsymbol={symbol}\nmagic=123456\nphase={ph}\n"
                    f"reason={'' if ph != 'ABORT' else 'ARM_TIMEOUT'}\n"
                    "lower=990.00000\nupper=1010.00000\n"
                    "t_arm_us=1\nt_trigger_us=2\nt_cancel_us=3\nt_burst_us=4\n",
                    encoding="ascii")
                await asyncio.sleep(0.02)
            stop_event.set()
        await asyncio.sleep(0.005)
    return rid


async def main():
    import uuid
    tmp = tempfile.mkdtemp(prefix="bridge_")
    os.environ["MT5_COMMON_FILES"] = tmp

    # Controlled ids so the fake EA can find the phase file.
    real_uuid4 = uuid.uuid4

    class FakeUUID:
        """uuid4() stand-in; the bridge reads .hex as an ATTRIBUTE."""

        def __init__(self, h):
            self.hex = h

    def with_id(h):
        return lambda: FakeUUID(h)

    from core.ea_bridge import EABridge, EABridgeError

    PLAN = {
        "lower": 990.0, "upper": 1010.0,
        "lines": [{"role": "PB", "slot": 1, "lot": 0.01, "tp": 1050.0,
                   "sl": 930.0, "tag": "ab12PB001"},
                  {"role": "PS", "slot": 1, "lot": 0.01, "tp": 0.0,
                   "sl": 0.0, "tag": "ab12PS001"},
                  {"role": "CS", "slot": 1, "lot": 0.01, "tp": 0.0,
                   "sl": 0.0, "tag": "ab12CS001"}],
    }

    print("\n[1] Happy path: ARMED -> TRIGGERED -> DONE")
    uuid.uuid4 = with_id("aaaa1111")
    bridge = EABridge()
    stop = asyncio.Event()
    task = asyncio.create_task(
        fake_ea(tmp, ["ARMED", "TRIGGERED", "DONE"], stop))
    seen = []
    result = await bridge.arm_limit(
        "FX Vol 20", 123456, PLAN, armed_timeout_ms=30000,
        win_fill_deadline_ms=1000, cancel_ack_deadline_ms=1000,
        on_phase=seen.append)
    await task
    check("terminal phase returned", result["phase"] == "DONE", str(result))
    check("interim phases delivered to on_phase",
          [p["phase"] for p in seen] == ["ARMED", "TRIGGERED", "DONE"],
          str([p["phase"] for p in seen]))
    check("phase file survives polling (non-destructive)",
          (Path(tmp) / "wt_arm_aaaa1111.txt").exists())
    check("stamps present in the terminal phase",
          all(k in result for k in ("t_trigger_us", "t_cancel_us", "t_burst_us")))

    print("\n[2] Terminal ABORT is returned, not raised")
    uuid.uuid4 = with_id("bbbb2222")
    stop = asyncio.Event()
    task = asyncio.create_task(
        fake_ea(tmp, ["ARMED", "ABORT"], stop))
    result = await bridge.arm_limit("FX Vol 20", 123456, PLAN,
                                    armed_timeout_ms=30000)
    await task
    check("ABORT returned as a terminal phase", result["phase"] == "ABORT")
    check("abort reason surfaced", result.get("reason") == "ARM_TIMEOUT",
          result.get("reason"))

    print("\n[3] Rejected ack raises immediately (no polling)")
    uuid.uuid4 = with_id("cccc3333")
    stop = asyncio.Event()
    task = asyncio.create_task(
        fake_ea(tmp, ["ARMED"], stop, accept=False, reason="BAD_LEVELS"))
    raised = False
    try:
        await bridge.arm_limit("FX Vol 20", 123456, PLAN,
                               armed_timeout_ms=30000)
    except EABridgeError as e:
        raised = True
        check("ack rejection message names the reason", "BAD_LEVELS" in str(e),
              str(e))
    # noqa: the fake EA sends reason= directly for a rejection
    await task
    check("rejected ack raises", raised)

    print("\n[4] Deadlines: a silent EA cannot hang arm_limit")
    uuid.uuid4 = with_id("dddd4444")
    stop = asyncio.Event()
    task = asyncio.create_task(fake_ea(tmp, [], stop))
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    raised = False
    try:
        await bridge.arm_limit("FX Vol 20", 123456, PLAN,
                               armed_timeout_ms=1000, win_fill_deadline_ms=100,
                               cancel_ack_deadline_ms=100,
                               armed_slack_s=0.5)
    except EABridgeError as e:
        raised = True
        check("timeout message names the deadline", "deadline" in str(e).lower()
              or "ARMED" in str(e), str(e))
    elapsed = loop.time() - t0
    stop.set()
    await task
    check("silent EA raises instead of hanging", raised)
    check("it raised within its own deadline (< 3s)", elapsed < 3.0,
          f"{elapsed:.2f}s")

    print("\n[5] cancel_all_pendings: EA path then direct fallback")
    pending = FakeOrders()
    _install_mt5_stub(pending)
    _O = sys.modules["MetaTrader5"]._make
    for i in range(3):
        pending.append(_O(1000 + i, "FX Vol 20", 123456))
    pending.append(_O(2000, "FX Vol 40", 123456))     # another symbol: untouched
    pending.append(_O(3000, "FX Vol 20", 999999))     # other magic: untouched

    import importlib
    import core.ea_bridge as eam
    importlib.reload(eam)

    async def no_ea(*a, **k):
        raise eam.EABridgeError("EA down")

    eam.EABridge._roundtrip = no_ea
    bridge2 = eam.EABridge()
    ok = await bridge2.cancel_all_pendings("FX Vol 20", 123456,
                                           deadline_s=4.0)
    check("sweep completed", ok)
    remaining_same = [o for o in pending
                      if o.sym == "FX Vol 20" and o.magic == 123456]
    check("all matching pendings removed", not remaining_same,
          str([o.ticket for o in remaining_same]))
    check("another symbol's pending untouched",
          any(o.ticket == 2000 for o in pending),
          str([o.ticket for o in pending]))
    check("another magic's pending untouched",
          any(o.ticket == 3000 for o in pending),
          str([o.ticket for o in pending]))

    print("\n[6] abort_arm falls back to the direct sweep when the EA is down")
    pending.append(_O(4000, "FX Vol 20", 123456))
    res = await bridge2.abort_arm("FX Vol 20", 123456)
    check("abort_arm returned a result", res is not None)
    check("pending swept even with the EA down",
          not [o for o in pending if o.sym == "FX Vol 20" and o.magic == 123456])

    uuid.uuid4 = real_uuid4
    print("\n" + "=" * 62)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))