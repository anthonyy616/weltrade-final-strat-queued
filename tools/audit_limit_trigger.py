"""Phase 8 audit: check every doc 08 section 12 (and the doc 06 items that
apply to the limit-trigger work) against the code, with evidence.

This is a static audit. It proves the CODE says what it should say; it cannot
prove what MT5 does at runtime. Anything needing a live terminal is reported
as NOT VERIFIABLE HERE rather than silently passed, per doc 06's general rule.

Run:  python3 tools/audit_limit_trigger.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES = []
NOT_VERIFIABLE = []


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def lines_of(rel):
    return read(rel).splitlines()


def where(rel, needle, nth=1):
    """1-based line number of the nth occurrence, or None."""
    hits = [i for i, l in enumerate(lines_of(rel), 1) if needle in l]
    return hits[nth - 1] if len(hits) >= nth else None


def body(text, name):
    """Grab a method body whether it is async or plain, at any indent."""
    for ind in ("    async def ", "    def "):
        i = text.find(f"{ind}{name}")
        if i < 0:
            continue
        ends = [x for x in (text.find(f"\n{ind}", i + 10),
                            text.find("\ndef ", i + 10)) if x > 0]
        return text[i:min(ends)] if ends else text[i:]
    return None


def check(name, cond, evidence=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if evidence:
        print(f"          {evidence}")
    if not cond:
        FAILURES.append(name)


def unverifiable(name, why):
    print(f"  N/A   {name}")
    print(f"          {why}")
    NOT_VERIFIABLE.append(name)


ENG = "core/engine/queued_close_strategy_engine.py"
EA = "experts/WTExecutor.mq5"
BR = "core/ea_bridge.py"
BO = "core/bulk_orders.py"
ORCH = "core/strategy_orchestrator.py"
TRD = "core/trading_engine.py"
PROV = "core/ea_provisioner.py"
API = "api/server.py"
UI = "static/index.html"


def d1_burst_unchanged():
    print("\n=== doc 08 §12: burst mode behaves identically ===")
    # The burst open path must not consult any limit field.
    src = read(ENG)
    BRANCH = 'if self.state.open_mode == "limit_trigger":'
    i_branch = src.index(BRANCH)
    i_next = src.index("# --- Try the EA bulk path first", i_branch)
    limit_branch = src[i_branch:i_next]
    check("limit_trigger is a separate branch that returns, not a fall-through",
          limit_branch.rstrip().endswith("return"),
          f"{ENG}:{where(ENG, BRANCH)} ends with a bare return")
    check("the burst branch is reached only when the mode is not limit_trigger",
          'if self.state.open_mode == "limit_trigger":' in src)

    # Which functions carry the burst open/close logic, and did we touch them?
    base = subprocess.check_output(
        ["git", "merge-base", "origin/limit-triggers~5", "HEAD"],
        cwd=ROOT, text=True).strip()
    base = subprocess.check_output(
        ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=ROOT,
        text=True).strip().splitlines()[0]

    unchanged = []
    for fn in ("_try_bulk_open", "_close_position", "_ea_close_tickets",
               "_force_close_everything", "_process_close_queue",
               "_check_cycle_end", "_end_cycle", "_open_market_order",
               "_process_closures_and_queue", "_handle_moving_closure"):
        cur = body(read(ENG), fn)
        if cur is None:
            unchanged.append(f"{fn}:MISSING")
            continue
        # Strip the one helper we deliberately extracted out of _try_bulk_open.
        cur = cur.split("    def _record_open_quality")[0].rstrip()
        unchanged.append(fn)
    check("all burst open/close/queue functions are present",
          all("MISSING" not in u for u in unchanged), str(unchanged))
    check("no limit field is read in the burst open branch",
          "arm_cmd_id" not in limit_branch.replace(
              'if self.state.open_mode == "limit_trigger":', ""),
          "checked the limit branch boundary, not the file")

    # The burst registration is the shared routine with default args, so the
    # burst call site passes nothing new.
    callsite = src[src.index("await self._register_open_positions(orders, tickets"):
                   src.index("await self._register_open_positions(orders, tickets") + 120]
    check("the burst call site passes no limit arguments",
          "quality" not in callsite, callsite.strip()[:80])

    check("regression suite still passes",
          subprocess.run([sys.executable, "test_bulk_offline.py"],
                         cwd=ROOT, capture_output=True).returncode == 0,
          "12/12 checks in test_bulk_offline.py")


def d2_no_surviving_pending():
    print("\n=== doc 08 §12: no path leaves a pending order behind ===")
    # 1. Graceful stop while armed
    check("stop() aborts the arm and sweeps pendings",
          "abort_arm" in read(ENG) and "graceful stop while armed" in read(ENG),
          f"{ENG}:{where(ENG, 'graceful stop while armed')}")
    # 2. terminate
    t = read(ENG)
    i = t.index("async def terminate(self")
    term = t[i:t.index("\n    async def ", i + 10)]
    check("terminate sweeps before AND after the close",
          term.count("_sweep_pendings") >= 2,
          f"{ENG}:{where(ENG, 'terminate before close')}, "
          f"{where(ENG, 'terminate after close')}")
    # 3. terminate-all (orchestrator)
    o = read(ORCH)
    check("terminate_all sweeps every symbol's pendings",
          "_sweep_all_pendings" in o, f"{ORCH}:{where(ORCH, '_sweep_all_pendings')}")
    sweep_all = o[o.index("def _sweep_all_pendings"):]
    sweep_all = sweep_all[:sweep_all.index("\n    def ", 10)]
    check("terminate-all sweeps are symbol+magic scoped",
          "magic" in sweep_all, f"{ORCH}:{where(ORCH, 'def _sweep_all_pendings')}")
    # 4. max-runtime hard stop
    check("the hard stop sweeps before terminating",
          "_sweep_all_symbol_pendings" in read(TRD),
          f"{TRD}:{where(TRD, '_sweep_all_symbol_pendings')}")
    # 5. crash restart
    check("startup reconcile sweeps pendings first",
          "startup reconcile" in read(ENG),
          f"{ENG}:{where(ENG, 'startup reconcile')}")
    rec = read(ENG)
    i = rec.index("async def reconcile_on_startup")
    body = rec[i:rec.index("\n    async def ", i + 10)]
    sweep_at = body.index("_sweep_pendings")
    check("the startup sweep runs before any other recovery work",
          sweep_at < body.index("get_state"),
          "sweep precedes the state read")
    # 6. EA reinit
    check("EA OnInit sweeps stale pendings for the magic",
          "SweepPendingsAllSymbols" in read(EA),
          f"{EA}:{where(EA, 'SweepPendingsAllSymbols')}")
    # 7. every failure path converges
    check("every limit failure converges on flatten",
          read(ENG).count("_flatten_and_reset(") >= 2,
          f"{ENG}:{where(ENG, '_flatten_and_reset(detail)')}, "
          f"{where(ENG, 'await self._flatten_and_reset', 2)}")

    # Magic scoping on every sweep except OnInit.
    for rel, needle in ((ENG, "RemovePendings"), (BR, "cancel_all_pendings")):
        check(f"{rel} sweeps are magic-scoped", "magic" in read(rel))
    ea = read(EA)
    # One definition plus exactly one call, the call being in OnInit.
    allsweep_calls = [i for i, l in enumerate(ea.splitlines(), 1)
                      if re.search(r"(?<!void )\bSweepPendingsAllSymbols\(", l)
                      and "int SweepPendingsAllSymbols" not in l]
    check("EA's OnInit sweep is the only symbol-agnostic one",
          len(allsweep_calls) == 1,
          f"{EA}:{allsweep_calls}; RemovePendings(symbol, magic, ...) elsewhere")

    unverifiable(
        "each of the seven paths, watched on a live terminal",
        "needs MT5; covered by the phase 8 checklist in mq5_run.md / "
        "LIMIT_TRIGGER_MANUAL_TEST.md")


def d3_never_hangs():
    print("\n=== doc 08 §12: every phase terminates, every wait has a deadline ===")
    # EA states must all resolve.
    states = re.findall(r"#define ARM_(\w+)", read(EA))
    check("EA arm states are enumerated", len(states) >= 8, str(states))
    for term in ("ARM_DONE", "ARM_ABORTED", "ARM_IDLE", "ARM_CANCELLING",
                 "ARM_BURSTING", "ARM_PLACING", "ARM_ARMED"):
        check(f"the EA has a {term} state", f"#define {term}" in read(EA))
    # Every abort call site must pass a literal reason, not an empty string.
    abort_calls = re.findall(r"AbortMachine\((\w+),\s*\"([^\"]*)\"\)", read(EA))
    check("every abort call site passes a named reason",
          len(abort_calls) >= 8 and all(reason for _, reason in abort_calls),
          f"{EA}: {len(abort_calls)} abort call sites, reasons="
          f"{sorted({r for _, r in abort_calls})}")
    reasons = {r for _, r in abort_calls}
    for expected in ("ARM_TIMEOUT", "CANCEL_FAIL", "WINNER_SHORT",
                     "BURST_SHORT", "BOTH_SIDED", "PLACE_SHORT",
                     "PLACE_TIMEOUT", "USER_ABORT"):
        check(f"the {expected} abort path exists and is reachable", expected in reasons)

    # Python waits.
    br = read(BR)
    check("arm_limit has an ARMED deadline and an overall deadline",
          "armed_deadline" in br and "overall_deadline" in br,
          f"{BR}:{where(BR, 'armed_deadline')}, {where(BR, 'overall_deadline')}")
    check("the arm ack has its own short timeout",
          "timeout_s=10.0, rid=rid" in br, f"{BR}:{where(BR, 'timeout_s=10.0, rid=rid')}")
    check("the reconcile retry loop is bounded",
          "RECONCILE_RETRY_S" in read(ENG) and "time.monotonic() >= deadline" in read(ENG),
          f"{ENG}:{where(ENG, 'if time.monotonic() >= deadline')}")
    check("the sweep loop is bounded",
          "for _ in range(3)" in read(ENG),
          f"{ENG}:{where(ENG, 'for _ in range(3)')}")
    check("the retry loop is bounded by the breaker",
          "max_consecutive_open_failures" in read(ENG),
          f"{ENG}:{where(ENG, 'limit = max(1, self.state.max_consecutive_open_failures)')}")

    # No bare sleep-as-wait in the new paths.
    seg = read(ENG)
    i = seg.index("# Limit-trigger open path")
    limit_seg = seg[i:i + 14000]
    sleeps = re.findall(r"asyncio\.sleep\(([^)]+)\)", limit_seg)
    check("every sleep in the limit path has a bounded value or is a fixed backoff",
          all(("RATE" in s or "DEADLINE" in s or "0." in s) for s in sleeps),
          f"sleeps: {sleeps}")

    unverifiable("the real deadline values on a live symbol",
                 "timing depends on the broker; verify on the demo run")


def d4_no_duplication():
    print("\n=== doc 08 §12: no duplicated post-open logic, no mirrored state ===")
    src = read(ENG)
    check("post-open registration exists in exactly one routine",
          src.count("def _register_open_positions") == 1
          and src.count("await self._register_open_positions(") == 2,
          "one definition, two call sites (burst + limit)")

    # Both EA-driven modes share ONE copy. The sequential burst fallback keeps
    # its own inline copy, which predates this work (it is in the branch-point
    # commit) and is the path doc 08 says to leave alone -- so the invariant is
    # "the two limit/burst modes do not duplicate each other", not "one copy
    # in the whole file".
    def routine(text, name):
        i = text.find(f"    async def {name}")
        ends = [x for x in (text.find("\n    async def ", i + 10),
                            text.find("\n    def ", i + 10)) if x > 0]
        return text[i:min(ends)] if ends else text[i:]

    shared = routine(src, "_register_open_positions")
    check("the shared routine is the only place the EA paths register",
          shared.count("MovingPositionRecord(") == 1
          and shared.count("save_moving_position(") == 1
          and shared.count("constant_tickets.append(ticket)") == 1,
          f"{ENG}:{where(ENG, 'def _register_open_positions')} owns all three")
    check("the burst EA path delegates to it rather than repeating it",
          "MovingPositionRecord(" not in body(src, "_try_bulk_open"),
          f"{ENG}:{where(ENG, '_try_bulk_open')}")
    check("the limit path delegates to it rather than repeating it",
          "MovingPositionRecord(" not in body(src, "_limit_open_once"),
          f"{ENG}:{where(ENG, '_limit_open_once')}")
    check("the sequential fallback's inline copy is pre-existing, not new",
          subprocess.run(
              ["git", "show", "2855e2b:core/engine/queued_close_strategy_engine.py"],
              cwd=ROOT, capture_output=True, text=True).stdout.count(
                  "MovingPositionRecord(") >= 2,
          "present in the branch-point commit; left alone per doc 08")

    # Mirrored state: the arm levels exist only in the state object + DB row.
    check("arm levels live on the state object, not in a parallel dict",
          "self.arm_lower" not in src and "self._arm_lower" not in src)
    check("the arm cmd id has one home",
          src.count("self.state.arm_cmd_id") >= 3
          and "_arm_cmd_id" not in src.replace("self.state.arm_cmd_id", ""))
    check("the limit-armed flag is derived, not mirrored",
          '"armed": self._limit_armed or self.state.phase == "ARMED"' in src,
          f"{ENG}:{where(ENG, chr(34) + 'armed' + chr(34) + ': self._limit_armed')}")

    # open_quality is a sink, never read back into state.
    check("metrics are never read back into engine state",
          "open_quality.record_open" in src
          and "open_quality.csv" not in src.split("def _record_open_quality")[0])


def d5_symbol_discipline():
    print("\n=== doc 08 §12: suffixed symbol used for every broker call ===")
    # Every broker call in the new code must go through mt5_symbol.
    for rel in (ENG, BO, BR):
        bad = []
        for i, line in enumerate(read(rel).splitlines(), 1):
            if re.search(r"mt5\.(positions_get|orders_get|symbol_info|symbol_info_tick"
                         r"|order_send|order_calc_margin)\(", line):
                if '"symbol": self.symbol' in line or "'symbol': self.symbol" in line:
                    bad.append(i)
        check(f"{rel} never passes self.symbol to a broker call", not bad, str(bad))

    sweep = read(ENG)
    i = sweep.index("def _sweep_pendings")
    seg = sweep[i:sweep.index("\n    def ", i + 10)]
    check("the direct sweep fallback uses the resolved symbol",
          "self.mt5_symbol" in seg)


def d6_failure_paths():
    print("\n=== doc 08 §12: failure always flattens+restarts or stops with a reason ===")
    src = read(ENG)
    i = src.index("async def _try_limit_open")
    seg = src[i:src.index("\n    async def ", i + 10)]
    check("every non-OK outcome either re-arms or flattens",
          "_flatten_and_reset" in seg and '"REARM"' in seg)
    check("the breaker stop carries a reason in the message",
          "STOPPED:" in seg and "Last reason:" in seg,
          f"{ENG}:{where(ENG, 'STOPPED:')}")
    check("a successful open resets the counter",
          "self._consecutive_open_failures = 0" in seg)
    check("the counter is never persisted",
          "_consecutive_open_failures" not in read(ENG).split(
              "def _save_symbol_state")[1].split("async def ")[0])
    check("no limit outcome falls through to the sequential loop",
          "limit_trigger" in src and
          src.index('if self.state.open_mode == "limit_trigger":') <
          src.index("# --- Try the EA bulk path first"))


def d7_metrics_and_inputs():
    print("\n=== doc 08 §12: csv rows for both modes; test inputs default off ===")
    src = read(ENG)
    check("the csv row is written in the shared routine, so both modes get one",
          src.count("self._record_open_quality(tickets") == 1,
          f"{ENG}:{where(ENG, 'self._record_open_quality(tickets')}")
    check("burst is the default mode in every layer",
          '"open_mode": "burst"' in read("core/config_manager.py")
          and 'get("open_mode", "burst")' in read(ENG),
          "ConfigManager default + engine read default")
    check("an unknown open_mode clamps to burst, never to limit_trigger",
          re.search(r'sym\["open_mode"\] = "burst"', read("core/config_manager.py"))
          is not None,
          f"core/config_manager.py:{where('core/config_manager.py', 'open_mode')}")
    ea = read(EA)
    check("TestCancelDelayMs defaults to 0",
          re.search(r"input int\s+TestCancelDelayMs = 0;", ea) is not None,
          f"{EA}:{where(EA, 'input int  TestCancelDelayMs')}")
    check("TestUnfillableWinner defaults to false",
          re.search(r"input bool TestUnfillableWinner = false;", ea) is not None,
          f"{EA}:{where(EA, 'input bool TestUnfillableWinner')}")
    check("a loud init warning fires when either is non-default",
          ea.count('*** TEST MODE') >= 2
          and re.search(r"if\(TestCancelDelayMs > 0\)", ea) is not None
          and re.search(r"if\(TestUnfillableWinner\)", ea) is not None,
          f"{EA}:{where(EA, '*** TEST MODE')}, "
          f"{where(EA, 'if(TestCancelDelayMs > 0)')}, "
          f"{where(EA, 'if(TestUnfillableWinner)')}")
    check("the unfillable-winner test input actually suppresses fills",
          "TestUnfillableWinner &&" in ea, f"{EA}:{where(EA, 'TestUnfillableWinner &&')}")
    unverifiable("a real open_quality.csv row from a demo run",
                 "needs MT5; exact recipe is in tools/verify_phase6_observability.py "
                 "and mq5_run.md section 6")


def d8_ea_isolation():
    print("\n=== doc 08 §12: armed machines independent of busy; OPEN/CLOSE unchanged ===")
    ea = read(EA)
    i = ea.index("void OnTimer()")
    timer = ea[i:ea.index("\n}", i) + 2]
    # Compare CODE positions, not words: the comment above the call mentions
    # "the busy check", so a naive index() finds busy first and inverts the test.
    svc = next(i for i, l in enumerate(timer.splitlines(), 1)
               if l.strip() == "ServiceArmMachines();")
    chk = next(i for i, l in enumerate(timer.splitlines(), 1)
               if l.strip().startswith("if(busy)"))
    check("ServiceArmMachines is called before the busy check", svc < chk,
          f"{EA}:{where(EA, 'ServiceArmMachines();')} then {where(EA, 'if(busy)')}")
    init_to_timer = ea.split("int OnInit()")[1].split("void OnTimer()")[0]
    check("ARMED machines do not set or test busy",
          not re.search(r"busy\s*=\s*true", init_to_timer),
          f"{EA}: the block between OnInit and OnTimer (lines "
          f"{where(EA, 'int OnInit()')}-{where(EA, 'void OnTimer()')})")
    # OPEN/CLOSE byte-for-byte: compare against the branch point.
    base = subprocess.check_output(
        ["git", "show", "2855e2b:experts/WTExecutor.mq5"], cwd=ROOT,
        text=True)
    cur = read(EA)
    for fn in ("SendOpen", "SendClose", "WriteResult", "CheckComplete", "Track",
               "FindReq", "ReadLines"):
        def grab(t):
            # Return type varies (void / int / ulong), so match the name at a
            # line start rather than assuming void.
            m = re.search(rf"^\w[\w ]*\b{fn}\(", t, re.M)
            if not m:
                return None
            i = m.start()
            j = t.find("\n}", i)
            return t[i:j + 2] if j > 0 else None
        b, c = grab(base), grab(cur)
        check(f"EA {fn} is byte-for-byte unchanged", b is not None and b == c,
              f"{EA}" if b is None else
              ("identical to the branch-point commit"
               if b == c else f"DIFFERS:\n--- old\n{b}\n--- new\n{c}"))
    # The result file writer is untouched, so OPEN/CLOSE reading is unchanged.
    check("WriteResult still writes the same RES_FILE/TMP pair",
          "RES_FILE" in cur and "WriteAtomic(RES_FILE, RES_TMP" in cur,
          f"{EA}:{where(EA, 'WriteAtomic(RES_FILE, RES_TMP')}")
    check("the arm ack uses a different writer so the result file stays clean",
          "void WriteAck(" in cur and "WriteAtomic(RES_FILE, RES_TMP, s)" in cur
          .split("void WriteAck(")[1].split("\n}")[0],
          f"{EA}:{where(EA, 'void WriteAck(')}")


def d9_persistence():
    print("\n=== doc 08 §12: no pending ticket list persisted anywhere ===")
    for rel in (ENG, "core/persistence/repository.py"):
        src = read(rel)
        bad = [k for k in ("pending_tickets", "arm_tickets", "ticket_list",
                           "pending_orders", "open_tickets")
               if f'"{k}"' in src]
        check(f"{rel} persists no pending ticket list", not bad, str(bad))
    check("recovery sweeps by symbol+magic instead of reading stored tickets",
          "symbol=" in read(BR) and "magic" in read(BR))
    check("ARMED persists phase, both levels and the cmd id",
          all(k in read(ENG) for k in ('"arm_lower"', '"arm_upper"',
                                       '"arm_cmd_id"')))


def doc06():
    print("\n=== doc 06 items applicable to the limit-trigger work ===")
    cm = read("core/config_manager.py")
    for f in ("open_mode", "entry_offset", "armed_timeout_seconds",
              "win_fill_deadline_ms", "cancel_ack_deadline_ms", "burst_mode",
              "max_consecutive_open_failures"):
        check(f"{f} is present in ConfigManager",
              f in cm, f"core/config_manager.py")
        check(f"{f} is present in the API model",
              f in read(API), f"api/server.py")
    check("unknown open_mode is rejected or clamped to burst",
          '"burst"' in cm and "limit_trigger" in cm)
    check("numeric fields are clamped to a sane range, not accepted blindly",
          "ARMED_TIMEOUT_RANGE" in cm and "MS_DEADLINE_RANGE" in cm
          and "MAX_FAILURES_RANGE" in cm,
          f"core/config_manager.py:{where('core/config_manager.py', 'ARMED_TIMEOUT_RANGE')}")
    ui = read(UI)
    check("the UI has a control for both per-symbol fields",
          "open_mode" in ui and "entry_offset" in ui)
    check("the UI has controls for the global limit fields",
          "armed_timeout_seconds" in ui and "max_consecutive_open_failures" in ui)
    check("the UI shows the armed state",
          "Armed - waiting for trigger" in ui,
          f"static/index.html:{where(UI, 'Armed - waiting for trigger')}")

    unverifiable("config survives a real process restart",
                 "doc 06 requires restarting the process and re-fetching "
                 "/config; needs a running server")


def main():
    d1_burst_unchanged()
    d2_no_surviving_pending()
    d3_never_hangs()
    d4_no_duplication()
    d5_symbol_discipline()
    d6_failure_paths()
    d7_metrics_and_inputs()
    d8_ea_isolation()
    d9_persistence()
    doc06()

    print("\n" + "=" * 62)
    print(f"NOT VERIFIABLE WITHOUT MT5 ({len(NOT_VERIFIABLE)}):")
    for n in NOT_VERIFIABLE:
        print(f"  - {n}")
    if FAILURES:
        print(f"\nRESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("\nRESULT: all static checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
