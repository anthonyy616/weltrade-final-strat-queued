#property version "1.20"
#property description "Command-driven async order executor (queued-close bot)"

input int InpPollMs      = 5;     // how often to look for a command file
input int InpReplyWaitMs = 8000;  // stop waiting for server replies after this

// --- Limit-trigger arm machines (doc 08 sections 6/7) ---
input long InpSweepMagic = 123456;   // magic swept on OnInit; must match the strategy
input ENUM_ORDER_TYPE_FILLING InpPendingFilling = ORDER_FILLING_RETURN; // pendings do NOT use FOK
input int  TestCancelDelayMs = 0;        // TEST ONLY: widen the losing-ladder cancel race
input bool TestUnfillableWinner = false; // TEST ONLY: winner never completes

#define CMD_FILE "wt_cmd.txt"
#define RES_FILE "wt_res.txt"
#define RES_TMP  "wt_res.tmp"
#define WT_EA_VERSION "1.2"
#define LOG_FILE  "wt_ea.log"

// Per-symbol phase files. The name carries the command id so a stale file from
// an earlier cycle can never be mistaken for this one (doc 08 section 6).
#define PFX      "wt_arm_"
#define PFX_EXT  ".txt"
#define PFX_TMP  "wt_arm.tmp"

#define MAX_ARM_MACHINES 4
#define ARM_SLOTS 256          // max orders per ladder; beyond this ARMLIMIT is rejected

// arm machine states
#define ARM_IDLE    0
#define ARM_PLACING 1
#define ARM_ARMED   2
#define ARM_ABORTED 3
#define ARM_DONE    4

// lanes inside a machine's request table
#define LANE_NONE 0
#define LANE_PB   1
#define LANE_PS   2
#define LANE_CS   3
#define LANE_CB   4
#define LANE_REM  5

// how long an abort sweep may keep the machine busy before it force-releases it
#define ARM_SWEEP_GRACE_MS 5000

// --- File logger (phase A): every Log() call also lands in <common>\Files\wt_ea.log ---
void Log(string msg)
{
   Print(msg);
   int h = FileOpen(LOG_FILE, FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE) return;
   FileSeek(h, 0, SEEK_END);
   FileWriteString(h, TimeToString(TimeLocal(), TIME_DATE|TIME_SECONDS) + " " + msg + "\r\n");
   FileClose(h);
}

bool   busy = false;
string cur_id = "", cur_action = "", cur_symbol = "";
long   cur_magic = 0;
ulong  t0 = 0, tSubmitEnd = 0, tFirst = 0, tLast = 0, deadline = 0;
int    sentOk = 0, sentFail = 0, repOk = 0, repFail = 0;
string failLines = "";

ulong  p_id[];
string p_tag[];
bool   p_done[];
int    p_count = 0;

// ---------------------------------------------------------------------------
// Limit-trigger arm machines (doc 08 sections 6/7).
//
// ONE machine per symbol, held OUTSIDE `busy`. `busy`, the command slot and
// wt_res.txt stay exclusive to OPEN/CLOSE, which are untouched. ARMLIMIT
// returns an immediate ack and clears busy; the machine then runs to
// DONE/ABORT on its own and reports through its own phase file.
//
// Parallel arrays rather than a struct-of-arrays: this matches the existing
// p_id[]/p_tag[]/p_done[] style and avoids MQL5 struct-with-array restrictions.
// ---------------------------------------------------------------------------
int    arm_state[MAX_ARM_MACHINES];
string arm_symbol[MAX_ARM_MACHINES];
long   arm_magic[MAX_ARM_MACHINES];
string arm_cmdid[MAX_ARM_MACHINES];
string arm_pfile[MAX_ARM_MACHINES];
double arm_lower[MAX_ARM_MACHINES];
double arm_upper[MAX_ARM_MACHINES];
int    arm_digits[MAX_ARM_MACHINES];
ulong  arm_armed_deadline[MAX_ARM_MACHINES];
ulong  arm_sweep_deadline[MAX_ARM_MACHINES];
int    arm_exp[MAX_ARM_MACHINES][4];   // [PB, PS, CS, CB] expected
int    arm_ok[MAX_ARM_MACHINES][4];    // [PB, PS, CS, CB] resolved OK
int    arm_sent[MAX_ARM_MACHINES];     // pending sends still unresolved
int    arm_rem_pending[MAX_ARM_MACHINES];
int    arm_trigger_side[MAX_ARM_MACHINES];  // 0 none, LANE_PB, LANE_PS
bool   arm_both_sided[MAX_ARM_MACHINES];
bool   arm_cancel_started[MAX_ARM_MACHINES];
bool   arm_burst_started[MAX_ARM_MACHINES];
ulong  arm_t_arm[MAX_ARM_MACHINES];
ulong  arm_t_trigger[MAX_ARM_MACHINES];
ulong  arm_t_cancel[MAX_ARM_MACHINES];
ulong  arm_t_burst[MAX_ARM_MACHINES];

int    arm_req_used[ARM_SLOTS][MAX_ARM_MACHINES];
ulong  arm_req_id[ARM_SLOTS][MAX_ARM_MACHINES];
int    arm_req_lane[ARM_SLOTS][MAX_ARM_MACHINES];
bool   arm_req_done[ARM_SLOTS][MAX_ARM_MACHINES];
ulong  arm_req_ticket[ARM_SLOTS][MAX_ARM_MACHINES];

string ArmStateName(int s)
{
   if(s == ARM_PLACING) return "PLACING";
   if(s == ARM_ARMED)   return "ARMED";
   if(s == ARM_ABORTED) return "ABORTED";
   if(s == ARM_DONE)    return "DONE";
   return "IDLE";
}

int LaneIndex(int lane)
{
   if(lane == LANE_PB) return 0;
   if(lane == LANE_PS) return 1;
   if(lane == LANE_CS) return 2;
   if(lane == LANE_CB) return 3;
   return -1;
}

void ResetArmMachine(int mi)
{
   arm_state[mi]         = ARM_IDLE;
   arm_symbol[mi]        = "";
   arm_magic[mi]         = 0;
   arm_cmdid[mi]         = "";
   arm_pfile[mi]         = "";
   arm_lower[mi]         = 0;
   arm_upper[mi]         = 0;
   arm_digits[mi]        = 8;
   arm_armed_deadline[mi]= 0;
   arm_sweep_deadline[mi]= 0;
   arm_sent[mi]          = 0;
   arm_rem_pending[mi]   = 0;
   arm_trigger_side[mi]  = 0;
   arm_both_sided[mi]    = false;
   arm_cancel_started[mi]= false;
   arm_burst_started[mi] = false;
   arm_t_arm[mi]         = 0;
   arm_t_trigger[mi]     = 0;
   arm_t_cancel[mi]      = 0;
   arm_t_burst[mi]       = 0;
   for(int l = 0; l < 4; l++) { arm_exp[mi][l] = 0; arm_ok[mi][l] = 0; }
   for(int s = 0; s < ARM_SLOTS; s++) arm_req_used[s][mi] = false;
}

int FindMachineBySymbol(const string sym)
{
   for(int i = 0; i < MAX_ARM_MACHINES; i++)
      if(arm_state[i] != ARM_IDLE && arm_symbol[i] == sym) return i;
   return -1;
}

// Request-table lookup. Scoped by (symbol, magic) via the machine that owns the
// entry, so a request belonging to an OPEN/CLOSE batch never matches here.
int FindMachineByRequest(const ulong req)
{
   for(int i = 0; i < MAX_ARM_MACHINES; i++)
   {
      if(arm_state[i] == ARM_IDLE) continue;
      for(int s = 0; s < ARM_SLOTS; s++)
         if(arm_req_used[s][i] && arm_req_id[s][i] == req && !arm_req_done[s][i])
            return i;
   }
   return -1;
}

int ArmTrack(int mi, ulong id, int lane)
{
   for(int s = 0; s < ARM_SLOTS; s++)
   {
      if(arm_req_used[s][mi]) continue;
      arm_req_used[s][mi]  = true;
      arm_req_id[s][mi]    = id;
      arm_req_lane[s][mi]  = lane;
      arm_req_done[s][mi]  = false;
      arm_req_ticket[s][mi]= 0;
      return s;
   }
   return -1;
}

void WriteAtomic(const string name, const string tmp, const string body)
{
   int h = FileOpen(tmp, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(h == INVALID_HANDLE) return;
   FileWriteString(h, body);
   FileClose(h);
   FileMove(tmp, FILE_COMMON, name, FILE_REWRITE|FILE_COMMON);
}

// Immediate ack for the arm commands. Deliberately a SEPARATE writer from
// WriteResult() so the OPEN/CLOSE result file stays byte-for-byte identical.
void WriteAck(bool accepted, const string reason)
{
   string s = "id=" + cur_id + "\n";
   s += "action=" + cur_action + "\n";
   s += "symbol=" + cur_symbol + "\n";
   s += "accepted=" + (accepted ? "1" : "0") + "\n";
   s += "reason=" + reason + "\n";
   s += "version=" + WT_EA_VERSION + "\n";
   WriteAtomic(RES_FILE, RES_TMP, s);
   Log("[LIMIT] ack id=" + cur_id + " action=" + cur_action + " symbol=" + cur_symbol
       + " accepted=" + (accepted ? "1" : "0") + " reason=" + reason);
   busy = false;
}

void WritePhase(int mi, const string phase, const string reason)
{
   ulong now = GetMicrosecondCount();
   ulong t0 = arm_t_arm[mi];
   string s = "id=" + arm_cmdid[mi] + "\n";
   s += "symbol=" + arm_symbol[mi] + "\n";
   s += "magic=" + IntegerToString(arm_magic[mi]) + "\n";
   s += "phase=" + phase + "\n";
   s += "reason=" + reason + "\n";
   s += "lower=" + DoubleToString(arm_lower[mi], arm_digits[mi]) + "\n";
   s += "upper=" + DoubleToString(arm_upper[mi], arm_digits[mi]) + "\n";
   s += "expected_buy=" + IntegerToString(arm_exp[mi][0] + arm_exp[mi][2]) + "\n";
   s += "expected_sell=" + IntegerToString(arm_exp[mi][1] + arm_exp[mi][3]) + "\n";
   s += "placed_pb=" + IntegerToString(arm_ok[mi][0]) + "\n";
   s += "placed_ps=" + IntegerToString(arm_ok[mi][1]) + "\n";
   s += "placed_cs=" + IntegerToString(arm_ok[mi][2]) + "\n";
   s += "placed_cb=" + IntegerToString(arm_ok[mi][3]) + "\n";
   s += "trigger_side=" + IntegerToString(arm_trigger_side[mi]) + "\n";
   s += "t_arm_us=" + IntegerToString(t0 > 0 ? (ulong)t0 : 0) + "\n";
   s += "t_trigger_us=" + IntegerToString(arm_t_trigger[mi]) + "\n";
   s += "t_cancel_us=" + IntegerToString(arm_t_cancel[mi]) + "\n";
   s += "t_burst_us=" + IntegerToString(arm_t_burst[mi]) + "\n";
   s += "elapsed_ms=" + DoubleToString(t0 > 0 ? (double)(now - t0) / 1000.0 : 0.0, 2) + "\n";
   s += "version=" + WT_EA_VERSION + "\n";
   WriteAtomic(arm_pfile[mi], PFX_TMP, s);
   Log("[LIMIT] phase id=" + arm_cmdid[mi] + " symbol=" + arm_symbol[mi]
       + " phase=" + phase + (reason == "" ? "" : " reason=" + reason)
       + " pb=" + IntegerToString(arm_ok[mi][0]) + "/" + IntegerToString(arm_exp[mi][0])
       + " ps=" + IntegerToString(arm_ok[mi][1]) + "/" + IntegerToString(arm_exp[mi][1]));
}

int OnInit()
{
   FileDelete(CMD_FILE, FILE_COMMON);
   FileDelete(RES_FILE, FILE_COMMON);
   for(int i = 0; i < MAX_ARM_MACHINES; i++) ResetArmMachine(i);
   // Nothing legitimate can be pending at init, so this is the ONE magic-wide
   // sweep (every other sweep is scoped to symbol+magic). doc 08 section 7.
   int swept = SweepPendingsAllSymbols(InpSweepMagic);
   if(swept > 0)
      Log("[LIMIT] OnInit swept " + IntegerToString(swept)
          + " stale pending order(s) for magic " + IntegerToString(InpSweepMagic));
   // Rotate the log if it grew past 5 MB (phase A, item 4)
   if(FileIsExist(LOG_FILE, FILE_COMMON))
   {
      int hrot = FileOpen(LOG_FILE, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
      if(hrot != INVALID_HANDLE)
      {
         ulong sz = FileSize(hrot);
         FileClose(hrot);
         if(sz > 5 * 1024 * 1024) FileDelete(LOG_FILE, FILE_COMMON);
      }
   }
   EventSetMillisecondTimer(InpPollMs);
   Log("WT executor v" + WT_EA_VERSION + " ready. Common files folder: "
       + TerminalInfoString(TERMINAL_COMMONDATA_PATH) + "\\Files");
   // Loud warning when a test-only input is left on (doc 08 section 7/12).
   if(TestCancelDelayMs > 0)
      Log("[LIMIT] *** TEST MODE: TestCancelDelayMs=" + IntegerToString(TestCancelDelayMs)
          + " -- MUST be 0 in production ***");
   if(TestUnfillableWinner)
      Log("[LIMIT] *** TEST MODE: TestUnfillableWinner=true -- MUST be false in production ***");
   if(InpPendingFilling != ORDER_FILLING_RETURN)
      Log("[LIMIT] pending filling policy is " + EnumToString(InpPendingFilling)
          + ", not the default ORDER_FILLING_RETURN");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
   // Arm machines are serviced BEFORE the busy check so another symbol's
   // in-flight OPEN can never delay an armed symbol's deadline or its ABORTARM.
   ServiceArmMachines();
   if(busy) { CheckComplete(); return; }
   if(!FileIsExist(CMD_FILE, FILE_COMMON)) return;
   RunCommand();
}

int ReadLines(string name, string &out[])
{
   ArrayResize(out, 0);
   int h = FileOpen(name, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(h == INVALID_HANDLE) return 0;
   while(!FileIsEnding(h))
   {
      string s = FileReadString(h);
      StringTrimLeft(s);
      StringTrimRight(s);
      if(StringLen(s) == 0) continue;
      int n = ArraySize(out);
      ArrayResize(out, n + 1);
      out[n] = s;
   }
   FileClose(h);
   return ArraySize(out);
}

void Track(ulong id, string tag)
{
   if(p_count >= ArraySize(p_id))
   {
      int n = p_count + 64;
      ArrayResize(p_id, n);
      ArrayResize(p_tag, n);
      ArrayResize(p_done, n);
   }
   p_id[p_count] = id;
   p_tag[p_count] = tag;
   p_done[p_count] = false;
   p_count++;
}

int FindReq(ulong id)
{
   for(int i = 0; i < p_count; i++)
      if(p_id[i] == id) return i;
   return -1;
}

// ---------------------------------------------------------------------------
// Pending-order removal (doc 08 section 7)
//
// RemovePendings() is ALWAYS scoped to (symbol, magic): the magic is shared
// across symbols, so a magic-wide sweep would cancel another symbol's live
// armed ladder. Only OnInit sweeps magic-wide.
// If mi >= 0 the removes are tracked in that machine's request table so the
// machine can wait for them; otherwise they are fire-and-forget (Python
// verifies with orders_get).
// ---------------------------------------------------------------------------
int RemovePendings(const string sym, const long mg, int mi)
{
   int submitted = 0;
   int total = OrdersTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      ulong tkt = OrderGetTicket(i);   // also selects the order
      if(tkt == 0) continue;
      if(OrderGetString(ORDER_SYMBOL) != sym) continue;
      if(OrderGetInteger(ORDER_MAGIC) != mg) continue;

      MqlTradeRequest rq; MqlTradeResult rs;
      ZeroMemory(rq); ZeroMemory(rs);
      rq.action   = TRADE_ACTION_REMOVE;
      rq.symbol   = sym;
      rq.position = tkt;
      rq.magic    = mg;
      rq.comment  = "arm-sweep";
      if(!OrderSendAsync(rq, rs))
      {
         Log("[LIMIT] remove rejected tkt=" + IntegerToString((long)tkt)
             + " retcode=" + IntegerToString((int)rs.retcode));
         continue;
      }
      submitted++;
      if(mi >= 0 && ArmTrack(mi, rs.request_id, LANE_REM) >= 0)
         arm_rem_pending[mi]++;
   }
   return submitted;
}

// The ONLY magic-wide sweep: used from OnInit, where nothing may legitimately
// be pending for any symbol.
int SweepPendingsAllSymbols(const long mg)
{
   int submitted = 0;
   int total = OrdersTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      ulong tkt = OrderGetTicket(i);
      if(tkt == 0) continue;
      if(OrderGetInteger(ORDER_MAGIC) != mg) continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      MqlTradeRequest rq; MqlTradeResult rs;
      ZeroMemory(rq); ZeroMemory(rs);
      rq.action   = TRADE_ACTION_REMOVE;
      rq.symbol   = sym;
      rq.position = tkt;
      rq.magic    = mg;
      rq.comment  = "init-sweep";
      if(OrderSendAsync(rq, rs)) submitted++;
      else
         Log("[LIMIT] init sweep rejected " + sym + " tkt="
             + IntegerToString((long)tkt) + " retcode=" + IntegerToString((int)rs.retcode));
   }
   return submitted;
}

// ---------------------------------------------------------------------------
// Arm command handlers
// ---------------------------------------------------------------------------

double HeaderDouble(string &lines[], int n, const string key, double def)
{
   for(int i = 0; i < n; i++)
   {
      int eq = StringFind(lines[i], "=");
      if(eq <= 0) continue;
      if(StringSubstr(lines[i], 0, eq) != key) continue;
      return StringToDouble(StringSubstr(lines[i], eq + 1));
   }
   return def;
}

void HandleArmLimit(string &lines[], int n)
{
   double armed_timeout_ms = HeaderDouble(lines, n, "armed_timeout_ms", 120000);
   double lower = HeaderDouble(lines, n, "lower", 0);
   double upper = HeaderDouble(lines, n, "upper", 0);

   if(armed_timeout_ms < 1000) armed_timeout_ms = 1000;
   if(armed_timeout_ms > 3600000) armed_timeout_ms = 3600000;

   if(!SymbolSelect(cur_symbol, true))
   {
      WriteAck(false, "SYMBOL_UNAVAILABLE");
      return;
   }
   MqlTick tk;
   if(!SymbolInfoTick(cur_symbol, tk))
   {
      WriteAck(false, "NO_TICK");
      return;
   }
   if(lower <= 0.0 || upper <= 0.0 || lower >= upper)
   {
      WriteAck(false, "BAD_LEVELS");
      return;
   }

   // One machine per symbol: a second ARMLIMIT for a live symbol is rejected
   // rather than silently replacing the first (doc 08 section 5).
   int existing = FindMachineBySymbol(cur_symbol);
   if(existing >= 0)
   {
      Log("[LIMIT] ARMLIMIT rejected: " + cur_symbol
          + " already armed by id=" + arm_cmdid[existing]);
      WriteAck(false, "SYMBOL_ALREADY_ARMED");
      return;
   }

   int mi = -1;
   for(int i = 0; i < MAX_ARM_MACHINES; i++)
      if(arm_state[i] == ARM_IDLE) { mi = i; break; }
   if(mi < 0)
   {
      WriteAck(false, "NO_FREE_MACHINE");
      return;
   }

   int digits = (int)SymbolInfoInteger(cur_symbol, SYMBOL_DIGITS);
   ResetArmMachine(mi);
   arm_state[mi]         = ARM_PLACING;
   arm_symbol[mi]        = cur_symbol;
   arm_magic[mi]         = cur_magic;
   arm_cmdid[mi]         = cur_id;
   arm_lower[mi]         = lower;
   arm_upper[mi]         = upper;
   arm_digits[mi]        = digits;
   arm_armed_deadline[mi]= GetTickCount64() + (ulong)armed_timeout_ms;
   arm_t_arm[mi]         = GetMicrosecondCount();
   arm_pfile[mi]         = PFX + cur_id + PFX_EXT;

   int placed_any = 0;
   int entries = 0;   // request-table slots consumed by THIS arm command
   for(int i = 0; i < n; i++)
   {
      string parts[];
      int np = StringSplit(lines[i], '|', parts);
      if(np < 6) continue;
      int lane;
      if(parts[0] == "PB")      lane = LANE_PB;
      else if(parts[0] == "PS") lane = LANE_PS;
      else if(parts[0] == "CS") lane = LANE_CS;
      else if(parts[0] == "CB") lane = LANE_CB;
      else continue;

      int li = LaneIndex(lane);
      if(li < 0) continue;
      arm_exp[mi][li]++;

      // The request table is shared by both ladders AND by the abort sweep,
      // so the cap is on the TOTAL entries this command consumes, not per lane.
      if(++entries > ARM_SLOTS)
      {
         Log("[LIMIT] ARMLIMIT rejected: " + cur_symbol + " needs "
             + IntegerToString(entries) + " request slots, ARM_SLOTS is "
             + IntegerToString(ARM_SLOTS));
         RemovePendings(cur_symbol, cur_magic, mi);
         ResetArmMachine(mi);
         WriteAck(false, "TOO_MANY_ORDERS");
         return;
      }

      // PB is a buy limit at the lower level; PS is a sell limit at the upper
      // level. CS/CB are contingent MARKET orders and are NOT placed here.
      if(lane == LANE_PB || lane == LANE_PS)
      {
         double price = (lane == LANE_PB) ? lower : upper;
         if(SendPending(mi, parts, lane, price, digits)) placed_any++;
      }
   }

   if(placed_any == 0 || (arm_exp[mi][0] == 0 && arm_exp[mi][1] == 0))
   {
      // Nothing placeable arrived: fail the ack rather than arm an empty ladder.
      RemovePendings(cur_symbol, cur_magic, mi);
      ResetArmMachine(mi);
      WriteAck(false, "NO_PLACABLE_ORDERS");
      return;
   }

   // Accepted. busy is cleared by WriteAck; the machine now runs on its own and
   // reports through its phase file. Python sends nothing but ABORTARM from here.
   WriteAck(true, "ARMING");
}

void HandleAbortArm()
{
   int mi = FindMachineBySymbol(cur_symbol);
   if(mi < 0)
   {
      WriteAck(false, "NOT_ARMED");
      return;
   }
   AbortMachine(mi, "USER_ABORT");
   WriteAck(true, "ABORTING");
}

void HandleCancelAll()
{
   int n = RemovePendings(cur_symbol, cur_magic, -1);
   WriteAck(true, "SUBMITTED_" + IntegerToString(n));
}

// Place one pending order asynchronously and track the request.
bool SendPending(int mi, string &parts[], int lane, double price, int digits)
{
   string tag = parts[5];
   double lot = StringToDouble(parts[2]);
   double tp  = StringToDouble(parts[3]);
   double sl  = StringToDouble(parts[4]);

   // Test hook: push one order of each ladder far past the level so the winner
   // can never complete (doc 08 section 7).
   if(TestUnfillableWinner && parts[0] == "PB")
      price = NormalizeDouble(price - 5.0 * _Point * 1000, digits);

   MqlTradeRequest rq; MqlTradeResult rs;
   ZeroMemory(rq); ZeroMemory(rs);
   rq.action       = TRADE_ACTION_PENDING;
   rq.symbol       = arm_symbol[mi];
   rq.volume       = lot;
   rq.type         = (lane == LANE_PB) ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT;
   rq.price        = NormalizeDouble(price, digits);
   rq.tp           = (tp > 0.0) ? NormalizeDouble(tp, digits) : 0.0;
   rq.sl           = (sl > 0.0) ? NormalizeDouble(sl, digits) : 0.0;
   rq.deviation    = 0;              // ignored for pendings
   rq.magic        = arm_magic[mi];
   rq.type_time    = ORDER_TIME_GTC;
   rq.type_filling = InpPendingFilling;   // NOT FOK -- pendings use RETURN/IOC
   rq.comment      = tag;

   if(!OrderSendAsync(rq, rs))
   {
      Log("[LIMIT] pending send rejected tag=" + tag + " lane=" + IntegerToString(lane)
          + " retcode=" + IntegerToString((int)rs.retcode));
      return false;
   }
   int slot = ArmTrack(mi, rs.request_id, lane);
   if(slot < 0)
   {
      Log("[LIMIT] request table full, untracking tag=" + tag);
      return false;
   }
   arm_sent[mi]++;
   return true;
}

// Terminal abort: report the phase FIRST so Python is never left waiting on a
// slow sweep, then keep the machine alive until the sweep resolves.
void AbortMachine(int mi, const string reason)
{
   if(arm_state[mi] == ARM_IDLE) return;
   WritePhase(mi, "ABORT", reason);
   arm_state[mi] = ARM_ABORTED;
   arm_sweep_deadline[mi] = GetTickCount64() + ARM_SWEEP_GRACE_MS;
   int n = RemovePendings(arm_symbol[mi], arm_magic[mi], mi);
   Log("[LIMIT] abort id=" + arm_cmdid[mi] + " symbol=" + arm_symbol[mi]
       + " reason=" + reason + " sweep_submitted=" + IntegerToString(n));
}

// PLACING -> ARMED once every send has resolved, or ABORT PLACE_SHORT.
void CheckPlacingDone(int mi)
{
   if(arm_state[mi] != ARM_PLACING) return;
   if(arm_sent[mi] > 0) return;   // still waiting on request results

   bool short_buy  = arm_ok[mi][0] < arm_exp[mi][0];
   bool short_sell = arm_ok[mi][1] < arm_exp[mi][1];
   if(short_buy || short_sell)
   {
      AbortMachine(mi, "PLACE_SHORT");
      return;
   }
   arm_state[mi] = ARM_ARMED;
   WritePhase(mi, "ARMED", "");
}

// Deadlines and abort completion. Called from OnTimer BEFORE the busy check,
// so nothing here can be gated by another symbol's in-flight batch.
void ServiceArmMachines()
{
   for(int i = 0; i < MAX_ARM_MACHINES; i++)
   {
      if(arm_state[i] == ARM_PLACING)
      {
         CheckPlacingDone(i);
         // A placement request that never gets a result must not wedge the
         // machine: the armed deadline doubles as the placement deadline.
         if(arm_state[i] == ARM_PLACING && GetTickCount64() >= arm_armed_deadline[i])
            AbortMachine(i, "PLACE_TIMEOUT");
         continue;
      }

      if(arm_state[i] == ARM_ARMED)
      {
         // No trigger logic yet (phase 3): any fill here is deliberately not
         // acted on. The armed timeout still fires and sweeps.
         if(GetTickCount64() >= arm_armed_deadline[i])
            AbortMachine(i, "ARM_TIMEOUT");
         continue;
      }

      if(arm_state[i] == ARM_ABORTED || arm_state[i] == ARM_DONE)
      {
         // Release the machine once its removes have resolved, or force-release
         // after the grace period so a stuck request cannot wedge a symbol.
         if(arm_rem_pending[i] <= 0 || GetTickCount64() >= arm_sweep_deadline[i])
         {
            Log("[LIMIT] machine released id=" + arm_cmdid[i] + " symbol=" + arm_symbol[i]
                + " rem_pending=" + IntegerToString(arm_rem_pending[i]));
            ResetArmMachine(i);
         }
         continue;
      }
   }
}

void RunCommand()
{
   string lines[];
   int n = ReadLines(CMD_FILE, lines);
   FileDelete(CMD_FILE, FILE_COMMON);
   if(n == 0) return;

   cur_id = ""; cur_action = ""; cur_symbol = ""; cur_magic = 0;
   sentOk = 0; sentFail = 0; repOk = 0; repFail = 0;
   p_count = 0; failLines = "";
   tFirst = 0; tLast = 0; tSubmitEnd = 0; t0 = 0;
   busy = true;

   for(int i = 0; i < n; i++)
   {
      int eq = StringFind(lines[i], "=");
      if(eq <= 0) continue;
      string k = StringSubstr(lines[i], 0, eq);
      string v = StringSubstr(lines[i], eq + 1);
      if(k == "id")          cur_id = v;
      else if(k == "action") cur_action = v;
      else if(k == "symbol") cur_symbol = v;
      else if(k == "magic")  cur_magic = StringToInteger(v);
   }

   // Command received: log id/action/symbol and the raw order lines (ASCII only)
   Log("cmd id=" + cur_id + " action=" + cur_action + " symbol=" + cur_symbol
       + " lines=" + IntegerToString(n));

   if(cur_action == "PING" || cur_symbol == "")
   {
      WriteResult(true);
      return;
   }

   // Limit-trigger arm commands: handled BEFORE the shared tick check because
   // each one resolves its own symbol/tick, and each returns without holding
   // `busy` for the armed window. The OPEN/CLOSE paths below are untouched.
   if(cur_action == "ARMLIMIT")
   {
      HandleArmLimit(lines, n);
      return;
   }
   if(cur_action == "ABORTARM")
   {
      HandleAbortArm();
      return;
   }
   if(cur_action == "CANCELALL")
   {
      HandleCancelAll();
      return;
   }

   MqlTick tk;
   if(!SymbolSelect(cur_symbol, true) || !SymbolInfoTick(cur_symbol, tk))
   {
      Log("FAIL symbol=" + cur_symbol + " reason=no tick or symbol unavailable");
      failLines += "F|TICK|0\n";
      WriteResult(true);
      return;
   }
   int digits = (int)SymbolInfoInteger(cur_symbol, SYMBOL_DIGITS);

   t0 = GetMicrosecondCount();
   if(cur_action == "CLOSEALL")
   {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong tkt = PositionGetTicket(i);
         if(tkt == 0) continue;
         if(PositionGetString(POSITION_SYMBOL) != cur_symbol) continue;
         if(PositionGetInteger(POSITION_MAGIC) != cur_magic) continue;
         SendClose(tkt, tk);
      }
   }
   else
   {
      for(int i = 0; i < n; i++)
      {
         string parts[];
         int np = StringSplit(lines[i], '|', parts);
         if(np < 2) continue;
         if(parts[0] == "O" && np >= 6) SendOpen(parts, tk, digits);
         else if(parts[0] == "T")       SendClose((ulong)StringToInteger(parts[1]), tk);
      }
   }
   tSubmitEnd = GetMicrosecondCount();
   deadline = GetTickCount64() + InpReplyWaitMs;
   CheckComplete();
}

void LogFailure(string tag, int retcode)
{
   Log("FAIL tag=" + tag + " retcode=" + IntegerToString(retcode));
}

void SendOpen(string &parts[], const MqlTick &tk, int digits)
{
   string tag = parts[5];
   bool buy = (parts[1] == "B");
   MqlTradeRequest rq; MqlTradeResult rs;
   ZeroMemory(rq); ZeroMemory(rs);
   rq.action       = TRADE_ACTION_DEAL;
   rq.symbol       = cur_symbol;
   rq.volume       = StringToDouble(parts[2]);
   rq.type         = buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   rq.price        = buy ? tk.ask : tk.bid;
   rq.tp           = NormalizeDouble(StringToDouble(parts[3]), digits);
   rq.sl           = NormalizeDouble(StringToDouble(parts[4]), digits);
   rq.deviation    = 200;
   rq.magic        = cur_magic;
   rq.type_filling = ORDER_FILLING_FOK;
   rq.comment      = tag;
   if(OrderSendAsync(rq, rs)) { sentOk++; Track(rs.request_id, tag); }
   else
   {
      sentFail++;
      failLines += "F|" + tag + "|" + IntegerToString((int)rs.retcode) + "\n";
      LogFailure(tag, (int)rs.retcode);
   }
}

void SendClose(ulong ticket, const MqlTick &tk)
{
   string tag = "T" + IntegerToString((long)ticket);
   if(!PositionSelectByTicket(ticket))
   {
      sentFail++;
      failLines += "F|" + tag + "|-1\n";   // -1 = position not found (already closed?)
      LogFailure(tag, -1);
      return;
   }
   bool wasBuy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   MqlTradeRequest rq; MqlTradeResult rs;
   ZeroMemory(rq); ZeroMemory(rs);
   rq.action       = TRADE_ACTION_DEAL;
   rq.symbol       = cur_symbol;
   rq.position     = ticket;
   rq.volume       = PositionGetDouble(POSITION_VOLUME);
   rq.type         = wasBuy ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   rq.price        = wasBuy ? tk.bid : tk.ask;
   rq.deviation    = 200;
   rq.magic        = cur_magic;
   rq.type_filling = ORDER_FILLING_FOK;
   rq.comment      = "close";
   if(OrderSendAsync(rq, rs)) { sentOk++; Track(rs.request_id, tag); }
   else
   {
      sentFail++;
      failLines += "F|" + tag + "|" + IntegerToString((int)rs.retcode) + "\n";
      LogFailure(tag, (int)rs.retcode);
   }
}

void OnTradeTransaction(const MqlTradeTransaction &t,
                        const MqlTradeRequest &rq,
                        const MqlTradeResult &rs)
{
   // Arm machines are demultiplexed FIRST and never fall through to the
   // OPEN/CLOSE bookkeeping, which stays exactly as it was.
   if(t.type == TRADE_TRANSACTION_REQUEST)
   {
      int mi = FindMachineByRequest(rs.request_id);
      if(mi >= 0)
      {
         for(int s = 0; s < ARM_SLOTS; s++)
         {
            if(!arm_req_used[s][mi] || arm_req_id[s][mi] != rs.request_id) continue;
            if(arm_req_done[s][mi]) break;
            arm_req_done[s][mi] = true;
            arm_req_ticket[s][mi] = rs.order;
            int lane = arm_req_lane[s][mi];
            if(rs.retcode == TRADE_RETCODE_DONE)
            {
               if(lane == LANE_REM)
               {
                  if(arm_rem_pending[mi] > 0) arm_rem_pending[mi]--;
               }
               else
               {
                  int li = LaneIndex(lane);
                  if(li >= 0) arm_ok[mi][li]++;
               }
            }
            else
            {
               Log("[LIMIT] request failed id=" + arm_cmdid[mi] + " lane="
                   + IntegerToString(lane) + " retcode=" + IntegerToString((int)rs.retcode));
               if(lane == LANE_REM && arm_rem_pending[mi] > 0) arm_rem_pending[mi]--;
            }
            if(arm_sent[mi] > 0) arm_sent[mi]--;
            break;
         }
         return;
      }
   }

   if(!busy || t.type != TRADE_TRANSACTION_REQUEST) return;
   int idx = FindReq(rs.request_id);
   if(idx < 0 || p_done[idx]) return;
   p_done[idx] = true;
   ulong now = GetMicrosecondCount();
   if(tFirst == 0) tFirst = now;
   tLast = now;
   if(rs.retcode == TRADE_RETCODE_DONE) repOk++;
   else
   {
      repFail++;
      failLines += "F|" + p_tag[idx] + "|" + IntegerToString((int)rs.retcode) + "\n";
      LogFailure(p_tag[idx], (int)rs.retcode);
   }
}

void CheckComplete()
{
   bool all = (repOk + repFail) >= sentOk;
   if(all || GetTickCount64() >= deadline)
   {
      if(!all)
         Log("REPLY TIMEOUT: " + IntegerToString(sentOk - repOk - repFail)
             + " of " + IntegerToString(sentOk) + " requests unanswered");
      WriteResult(all);
   }
}

void WriteResult(bool complete)
{
   string s = "id=" + cur_id + "\n";
   s += "action=" + cur_action + "\n";
   s += "complete=" + (complete ? "1" : "0") + "\n";
   s += "sent_ok=" + IntegerToString(sentOk) + "\n";
   s += "sent_fail=" + IntegerToString(sentFail) + "\n";
   s += "replies_ok=" + IntegerToString(repOk) + "\n";
   s += "replies_fail=" + IntegerToString(repFail) + "\n";
   s += "submit_ms=" + DoubleToString((tSubmitEnd - t0) / 1000.0, 2) + "\n";
   s += "first_reply_ms=" + DoubleToString(tFirst > 0 ? (tFirst - t0) / 1000.0 : 0.0, 2) + "\n";
   s += "last_reply_ms=" + DoubleToString(tLast > 0 ? (tLast - t0) / 1000.0 : 0.0, 2) + "\n";
   s += "version=" + WT_EA_VERSION + "\n";
   s += failLines;
   for(int i = 0; i < p_count; i++)
      if(!p_done[i]) s += "P|" + p_tag[i] + "\n";

   int h = FileOpen(RES_TMP, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, s);
      FileClose(h);
      FileMove(RES_TMP, FILE_COMMON, RES_FILE, FILE_REWRITE|FILE_COMMON);
   }
   Log("done id=" + cur_id + " action=" + cur_action
       + " sent_ok=" + IntegerToString(sentOk)
       + " replies_ok=" + IntegerToString(repOk)
       + " replies_fail=" + IntegerToString(repFail)
       + " submit_ms=" + DoubleToString(tSubmitEnd > t0 ? (tSubmitEnd - t0) / 1000.0 : 0.0, 2)
       + " last_reply_ms=" + DoubleToString(tLast > 0 ? (tLast - t0) / 1000.0 : 0.0, 2));
   busy = false;
}