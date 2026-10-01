#property version "1.10"
#property description "Command-driven async order executor (queued-close bot)"

input int InpPollMs      = 5;     // how often to look for a command file
input int InpReplyWaitMs = 8000;  // stop waiting for server replies after this

#define CMD_FILE "wt_cmd.txt"
#define RES_FILE "wt_res.txt"
#define RES_TMP  "wt_res.tmp"
#define WT_EA_VERSION "1.1"
#define LOG_FILE  "wt_ea.log"

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

int OnInit()
{
   FileDelete(CMD_FILE, FILE_COMMON);
   FileDelete(RES_FILE, FILE_COMMON);
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
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
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