import time
from concurrent.futures import ThreadPoolExecutor
import MetaTrader5 as mt5

SYMBOL, N, LOT, MAGIC = "FX Vol 20", 55, 0.01, 999001
FILLING = mt5.ORDER_FILLING_FOK   # if you get retcode 10030, try ORDER_FILLING_IOC

def send(req):
    r = mt5.order_send(req)
    return (None, 0, 0.0) if r is None else (r.retcode, r.order, r.price)

def build(tick):
    reqs = []
    for i in range(N):  # interleaved so neither side is systematically later
        for tag, typ, px in (("B", mt5.ORDER_TYPE_BUY, tick.ask),
                             ("S", mt5.ORDER_TYPE_SELL, tick.bid)):
            reqs.append({
                "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": LOT,
                "type": typ, "price": px, "deviation": 200, "magic": MAGIC,
                "comment": f"bench{tag}{i}", "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": FILLING,
            })
    return reqs

def close_all(pool):
    poss = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
    tick = mt5.symbol_info_tick(SYMBOL)
    reqs = []
    for p in poss:
        buy = p.type == mt5.ORDER_TYPE_BUY
        reqs.append({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "position": p.ticket,
            "volume": p.volume, "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if buy else tick.ask, "deviation": 200,
            "magic": MAGIC, "type_filling": FILLING,
        })
    list(pool.map(send, reqs))

def run(workers):
    tick = mt5.symbol_info_tick(SYMBOL)
    reqs = build(tick)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(send, reqs))
    ms = (time.perf_counter() - t0) * 1000
    ok = [(rq, o) for rq, o in zip(reqs, out) if o[0] == mt5.TRADE_RETCODE_DONE]
    buys = [o[2] for rq, o in ok if rq["type"] == mt5.ORDER_TYPE_BUY]
    sells = [o[2] for rq, o in ok if rq["type"] == mt5.ORDER_TYPE_SELL]
    codes = {}
    for o in out:
        codes[o[0]] = codes.get(o[0], 0) + 1
    print(f"workers={workers:>2} | {ms:7.0f} ms | filled {len(ok)}/{len(reqs)} | "
          f"buy range {min(buys, default=0):.3f}-{max(buys, default=0):.3f} | "
          f"sell range {min(sells, default=0):.3f}-{max(sells, default=0):.3f} | codes {codes}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        close_all(pool)

if __name__ == "__main__":
    assert mt5.initialize(), mt5.last_error()
    mt5.symbol_select(SYMBOL, True)
    for w in (1, 4, 8, 16, 32):
        run(w)
        time.sleep(2)
    mt5.shutdown()