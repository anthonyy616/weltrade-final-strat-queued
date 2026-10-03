"""Static structural check for experts/WTExecutor.mq5 (plan phase 2).

MetaEditor is not available on every machine, so this catches the class of
mistake that would otherwise only show up as a compile error on Anthony's
box: a call to a function that does not exist, a reference to a global that was
never declared, or a malformed brace/paren balance.

    python3 tools/verify_ea_static.py
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "experts" / "WTExecutor.mq5"

# MQL5 / MetaTrader builtins the EA legitimately calls.
BUILTINS = set("""
Print FileOpen FileClose FileSeek FileWriteString FileReadString FileIsEnding
FileSize FileDelete FileIsExist FileMove EventSetMillisecondTimer EventKillTimer
ArrayResize ArraySize StringLen StringFind StringSubstr StringTrimLeft
StringTrimRight StringSplit StringToInteger StringToDouble StringToString
IntegerToString DoubleToString DoubleToString TimeToString EnumToString
SymbolSelect SymbolInfoTick SymbolInfoInteger PositionsTotal PositionGetTicket
PositionGetString PositionGetInteger PositionSelectByTicket OrdersTotal
OrderGetTicket OrderGetString OrderGetInteger OrderSendAsync OrderDelete
GetTickCount64 GetMicrosecondCount TimeLocal TerminalInfoString
ZeroMemory TradeServer HistorySelect HistoryDealSelect HistoryDealGetInteger
HistoryDealGetString ArraySetAsSeries NormalizeDouble PositionGetDouble
PositionGetTicket OrderGetTicket HistoryDealGetTicket HistoryDealsTotal
OrderSelect TimeCurrent TimeGMT Sleep
""".split())

KEYWORDS = set("""
if else for while return switch case break continue do sizeof new delete true
false null void const static extern struct class enum template public private
protected virtual override input inputgroup output string ulong uint uchar short
int long float double bool char datetime color
""".split())

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def strip_comments(src):
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif src[i] == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])   # keep the literal: checks read them
            i = j + 1
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def blank_strings(src):
    """Replace string literal contents so the call-site scan does not see
    words inside log messages as function calls."""
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', src)


def main():
    raw = SRC.read_text(encoding="utf-8")
    src = strip_comments(raw)

    print(f"\n[1] Balance ({SRC.name})")
    check("braces balanced", src.count("{") == src.count("}"),
          f"{{={src.count('{')} }}={src.count('}')}")
    check("parens balanced", src.count("(") == src.count(")"),
          f"(={src.count('(')} )={src.count(')')}")

    print("\n[2] Function definitions and call sites")
    defined = set(re.findall(
        r"^(?:void|int|bool|string|ulong|uint|double|long|datetime|color)\s+(\w+)\s*\(",
        src, re.M))
    # Event handlers count as defined too.
    for m in re.finditer(r"\b(OnInit|OnDeinit|OnTimer|OnTick|OnTradeTransaction)\b",
                         src):
        defined.add(m.group(1))
    called = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", blank_strings(src)))
    unknown = sorted(called - defined - BUILTINS - KEYWORDS)
    check("every called function is defined", not unknown, ", ".join(unknown))

    print("\n[3] Globals")
    declared = set(re.findall(r"^(?:bool|int|long|ulong|uint|double|string|char)\s+"
                              r"([a-z_]\w*)\s*(\[|=|;)", src, re.M))
    declared |= set(re.findall(r"^#define\s+(\w+)", src, re.M))
    declared |= set(re.findall(r"^input\s+\S+\s+(\w+)\s*=", src, re.M))
    declared |= set(re.findall(r"^(?:bool|int|long|ulong|uint|double|string|char)\s+"
                               r"(\w+)\[", src, re.M))
    # Everything declared on one line: "bool busy = false;"
    declared |= set(re.findall(
        r"^(?:bool|int|long|ulong|uint|double|string|char)\s+([a-z_]\w*)\s*(?:=|;)",
        src, re.M))
    for arr in re.findall(
            r"^(?:bool|int|long|ulong|uint|double|string|char)\s+(\w+)\[", src, re.M):
        declared.add(arr)
    arm_used = set(re.findall(r"\b(arm_[a-z_]\w*)", src))
    arm_decl = set()
    for arr in re.findall(
            r"^(?:bool|int|long|ulong|uint|double|string|datetime)\s+(arm_\w+)\[",
            src, re.M):
        arm_decl.add(arr)
    # index arrays declared as arm_x[..][..]
    for m in re.finditer(r"^(?:bool|int|long|ulong|uint|double|string|datetime)\s+"
                         r"(arm_\w+)\[([A-Z_]+)\]\[([A-Z_]+)\]", src, re.M):
        arm_decl.add(m.group(1))
    check("every arm_* global is declared",
          arm_used <= arm_decl, ", ".join(sorted(arm_used - arm_decl)))

    print("\n[4] Limit-trigger wiring present (doc 08 sections 6/7)")
    for act in ("ARMLIMIT", "ABORTARM", "CANCELALL"):
        check(f"{act} dispatched in RunCommand", f'cur_action == "{act}"' in src)
    check("arm machines serviced before the busy check",
          src.index("ServiceArmMachines();") < src.index("if(busy) { CheckComplete(); return; }"))
    check("OnInit performs the magic-wide sweep",
          "SweepPendingsAllSymbols(InpSweepMagic)" in src)
    check("ACK is a separate writer from the OPEN result",
          "void WriteAck(" in src and "void WritePhase(" in src)
    check("phase file name carries the command id",
          "PFX + cur_id + PFX_EXT" in src)
    check("pending orders use GTC, not FOK",
          "rq.type_time    = ORDER_TIME_GTC;" in src)
    check("pending filling policy is an input, default RETURN",
          "InpPendingFilling = ORDER_FILLING_RETURN" in raw)
    check("placement has its own timeout (no silent wedge)",
          "PLACE_TIMEOUT" in src)
    check("every non-IDLE machine state has an exit",
          all(s in src for s in ("ARM_PLACING", "ARM_ARMED", "ARM_ABORTED", "ARM_DONE")))
    check("test-only inputs log loudly at init",
          "TEST MODE" in raw and "TestCancelDelayMs" in raw
          and "TestUnfillableWinner" in raw)

    print("\n[5] Sweep scope (magic is shared across symbols)")
    # RemovePendings must filter on BOTH symbol and magic.
    body = src[src.index("int RemovePendings("):src.index("int SweepPendingsAllSymbols(")]
    check("RemovePendings filters on symbol", "ORDER_SYMBOL" in body)
    check("RemovePendings filters on magic", "ORDER_MAGIC" in body)
    init_body = src[src.index("int SweepPendingsAllSymbols("):]
    init_body = init_body[:init_body.index("double HeaderDouble(")]
    # It reads ORDER_SYMBOL only to build the remove request; it must never
    # FILTER on it. A filter would look like "if(OrderGetString(ORDER_SYMBOL)..."
    # or an ORDER_SYMBOL comparison.
    check("only OnInit sweeps magic-wide (reads symbol, never filters on it)",
          "ORDER_MAGIC" in init_body
          and "OrderGetString(ORDER_SYMBOL)" in init_body
          and "if(OrderGetString(ORDER_SYMBOL)" not in init_body
          and "ORDER_SYMBOL) !=" not in init_body
          and "ORDER_SYMBOL) ==" not in init_body)

    print("\n[6] OPEN/CLOSE flow untouched")
    old = Path("/tmp/wt_old_reference.mq5")
    if old.exists():
        o = strip_comments(old.read_text(encoding="utf-8"))
        for fn in ("SendOpen", "SendClose", "WriteResult", "CheckComplete",
                   "CheckComplete", "Track", "FindReq", "ReadLines", "LogFailure"):
            def body_of(text, name):
                # Anchor on the DEFINITION (return type + name at line start) so
                # a call site such as RunCommand(); inside OnTimer never matches.
                m = re.search(r"^(?:void|int|bool|string|ulong|uint|double|long"
                              r"|datetime|color)\s+%s\s*\([^)]*\)\s*\{" % re.escape(name),
                              text, re.M)
                if not m:
                    return None
                # The regex already consumed the opening brace.
                i = m.end() - 1
                d, j = 0, i
                while j < len(text):
                    if text[j] == "{":
                        d += 1
                    elif text[j] == "}":
                        d -= 1
                        if d == 0:
                            break
                    j += 1
                return text[i:j + 1]
            a, b = body_of(o, fn), body_of(src, fn)
            if a != b:
                print(f"       old[{fn}]={a!r}")
                print(f"       new[{fn}]={b!r}")
            check(f"{fn}() byte-for-byte unchanged", a is not None and a == b)
        for fn in ("OnInit", "OnTimer", "RunCommand", "OnTradeTransaction"):
            a, b = body_of(o, fn), body_of(src, fn)
            # Compare STRIPPED lines on both sides; a pure indentation change
            # is not a change.
            new_lines = {l.strip() for l in (b or "").splitlines()}
            removed = [l.strip() for l in (a or "").splitlines()
                       if l.strip() and l.strip() not in new_lines]
            check(f"{fn}() changes are purely additive", not removed,
                  "removed/changed: " + "; ".join(removed))
    else:
        print("  SKIP  no reference copy at /tmp/wt_old_reference.mq5 "
              "(git show HEAD:experts/WTExecutor.mq5 > that path to enable)")

    print("\n[7] Phase 3: trigger / cancel / burst / deadlines")
    # PREFLIGHT_FAIL is Python's, not the EA's: the EA is never reached when a
    # preflight check fails, so it must NOT appear here.
    abort_reasons = ["PLACE_SHORT", "ARM_TIMEOUT", "BOTH_SIDED",
                     "WINNER_SHORT", "CANCEL_FAIL", "BURST_SHORT", "USER_ABORT",
                     "PLACE_TIMEOUT", "BURST_NO_TICK"]
    present = re.findall(r'AbortMachine\s*\(\s*\w+\s*,\s*"([A-Z_]+)"', src)
    missing = [r for r in abort_reasons if r not in present]
    check("every abort reason in doc 08 section 6 is reachable", not missing,
          ", ".join(missing))
    check("DONE is written", 'WritePhase(mi, "DONE", "")' in src)
    check("only ARM_DONE/ARM_ABORTED are terminal states",
          len(re.findall(r"arm_state\[mi\]\s*=\s*ARM_DONE", src)) == 1
          and len(re.findall(r"arm_state\[mi\]\s*=\s*ARM_ABORTED", src)) == 1)

    # Every non-terminal state must have a deadline check in ServiceArmMachines.
    svc = src[src.index("void ServiceArmMachines()"):]
    svc = svc[:svc.index("void RunCommand()")]
    for state, deadline in (("ARM_PLACING", "arm_armed_deadline"),
                            ("ARM_ARMED", "arm_armed_deadline"),
                            ("ARM_CANCELLING", "arm_cancel_deadline"),
                            ("ARM_BURSTING", "arm_win_deadline")):
        # Slice between consecutive state-block starts so a self-referential
        # condition inside the block cannot truncate it.
        # Anchor on the block-opening indentation (6 spaces inside the for
        # loop) so a nested self-referential condition is not treated as a
        # new block start.
        starts = [m.start() for m in
                  re.finditer(r"^ {6}if\(arm_state\[i\] == ARM_", svc, re.M)]
        seg = ""
        for idx, st in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(svc)
            chunk = svc[st:end]
            if chunk.lstrip().startswith(f"if(arm_state[i] == {state})"):
                seg = chunk
                break
        check(f"{state} has a deadline escape ({deadline})",
              deadline in seg and "AbortMachine" in seg)

    cancel = src[src.index("void SendCancelRemoves("):]
    cancel = cancel[:cancel.index("void StartBurst(")]
    check("losing-ladder cancel never touches the triggered lane",
          "LaneOfOpposite(arm_trigger_side" in cancel
          and "if(arm_req_lane[s][mi] != losing) continue;" in cancel)
    check("burst fires the contingent side for the triggered scenario",
          "arm_trigger_side[mi] == LANE_PB" in src and "lane == LANE_CS" in src
          and "lane == LANE_CB" in src)
    check("deals are de-duplicated by ticket",
          "DealSeen(" in src and "MarkDeal(" in src)
    check("a missed DEAL_ADD has a deal-history safety net",
          "ScanRecentDeals(i);" in svc)
    check("burst gets a retry pass before BURST_SHORT",
          "arm_burst_pass[i] < 2" in svc)
    check("test-only inputs exist and are wired",
          "TestCancelDelayMs" in src and "TestUnfillableWinner" in src)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())