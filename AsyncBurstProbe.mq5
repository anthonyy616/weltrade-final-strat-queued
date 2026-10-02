#property strict
input string InpSymbol  = "FX Vol 20";
input int    InpBuys    = 150;
input int    InpSells   = 150;
input double InpLot     = 0.01;
input long   InpMagic   = 999003;
input int    InpWaitSec = 10;

enum Stage { ARMED, FIRED, DONE };
Stage stage = ARMED;
ulong t0 = 0, tSubmitEnd = 0, tFirstFill = 0, tLastFill = 0;
int sentOk = 0, sentFail = 0, reqOk = 0, reqFail = 0, deals = 0;
double bMin = DBL_MAX, bMax = 0, sMin = DBL_MAX, sMax = 0;

int OnInit()
{
   SymbolSelect(InpSymbol, true);
   EventSetTimer(1);
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   if(stage == ARMED)
   {
      stage = FIRED;
      EventKillTimer();
      Fire();
      EventSetTimer(InpWaitSec);
      return;
   }
   if(stage == FIRED)
   {
      stage = DONE;
      EventKillTimer();
      Report();
      CloseAll();
   }
}

void Fire()
{
   MqlTick tk;
   SymbolInfoTick(InpSymbol, tk);
   int total = InpBuys + InpSells;
   int b = 0, s = 0;
   t0 = GetMicrosecondCount();
   for(int i = 0; i < total; i++)
   {
      bool buy = (b < InpBuys) && (b <= s || s >= InpSells);
      MqlTradeRequest rq; MqlTradeResult rs;
      ZeroMemory(rq); ZeroMemory(rs);
      rq.action       = TRADE_ACTION_DEAL;
      rq.symbol       = InpSymbol;
      rq.volume       = InpLot;
      rq.type         = buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      rq.price        = buy ? tk.ask : tk.bid;
      rq.deviation    = 200;
      rq.magic        = InpMagic;
      rq.type_filling = ORDER_FILLING_FOK;
      rq.comment      = "async";
      if(OrderSendAsync(rq, rs)) sentOk++; else sentFail++;
      if(buy) b++; else s++;
   }
   tSubmitEnd = GetMicrosecondCount();
}

void OnTradeTransaction(const MqlTradeTransaction &t,
                        const MqlTradeRequest &rq,
                        const MqlTradeResult &rs)
{
   if(stage != FIRED) return;
   if(t.type == TRADE_TRANSACTION_REQUEST)
   {
      if(rs.retcode == TRADE_RETCODE_DONE) reqOk++;
      else { reqFail++; Print("request failed, retcode=", rs.retcode); }
      return;
   }
   if(t.type == TRADE_TRANSACTION_DEAL_ADD && HistoryDealSelect(t.deal))
   {
      if(HistoryDealGetInteger(t.deal, DEAL_MAGIC) != InpMagic) return;
      ulong now = GetMicrosecondCount();
      if(tFirstFill == 0) { tFirstFill = now; }
      tLastFill = now;
      deals++;
      double p  = HistoryDealGetDouble(t.deal, DEAL_PRICE);
      long   ty = HistoryDealGetInteger(t.deal, DEAL_TYPE);
      if(ty == DEAL_TYPE_BUY)       { bMin = MathMin(bMin, p); bMax = MathMax(bMax, p); }
      else if(ty == DEAL_TYPE_SELL) { sMin = MathMin(sMin, p); sMax = MathMax(sMax, p); }
   }
}

void Report()
{
   Print("=== ASYNC BURST REPORT ===");
   Print("submitted ok/fail: ", sentOk, "/", sentFail,
         " | server replies ok/fail: ", reqOk, "/", reqFail, " | deals seen: ", deals);
   Print("submit loop: ", (tSubmitEnd - t0) / 1000.0, " ms");
   if(tFirstFill > 0)
      Print("first fill at ", (tFirstFill - t0) / 1000.0, " ms, last fill at ",
            (tLastFill - t0) / 1000.0, " ms (spread ", (tLastFill - tFirstFill) / 1000.0, " ms)");
   Print("buy prices ", bMin, " - ", bMax, " | sell prices ", sMin, " - ", sMax);
}

void CloseAll()
{
   for(int pass = 0; pass < 5; pass++)
   {
      bool any = false;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong tk = PositionGetTicket(i);
         if(tk == 0) continue;
         if(PositionGetString(POSITION_SYMBOL) != InpSymbol) continue;
         if(PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
         any = true;
         long ty = PositionGetInteger(POSITION_TYPE);
         MqlTick t; SymbolInfoTick(InpSymbol, t);
         MqlTradeRequest rq; MqlTradeResult rs;
         ZeroMemory(rq); ZeroMemory(rs);
         rq.action       = TRADE_ACTION_DEAL;
         rq.symbol       = InpSymbol;
         rq.position     = tk;
         rq.volume       = PositionGetDouble(POSITION_VOLUME);
         rq.type         = (ty == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
         rq.price        = (ty == POSITION_TYPE_BUY) ? t.bid : t.ask;
         rq.deviation    = 200;
         rq.magic        = InpMagic;
         rq.type_filling = ORDER_FILLING_FOK;
         OrderSend(rq, rs);
      }
      if(!any) { Print("cleanup finished, nothing left open"); return; }
      Sleep(500);
   }
   Print("cleanup gave up after 5 passes, check for leftover positions");
}