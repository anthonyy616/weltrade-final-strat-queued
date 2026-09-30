import time
import MetaTrader5 as mt5
from concurrent.futures import ThreadPoolExecutor


SYMBOL, LOT, MAGIC, N = "FX Vol 20", 0.01, 999002, 20
FILLING = mt5.ORDER_FILLING_FOK

def conn():
    ti = mt5.terminal_info()
    return (ti.connected, ti.ping_last) if ti else (None, None)

def market(typ):
    t = mt5.symbol_info_tick(SYMBOL)
    px = t.ask if typ == mt5.ORDER_TYPE_BUY else t.bid
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": LOT,
           "type": typ, "price": px, "deviation": 200, "magic": MAGIC,
           "comment": "probe", "type_time": mt5.ORDER_TIME_GTC,
           "type_filling": FILLING}
    t0 = time.perf_counter()
    r = mt5.order_send(req)
    return r, (time.perf_counter() - t0) * 1000

def wait_connected(limit=120):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit:
        c, _ = conn()
        if c:
            return time.perf_counter() - t0
        time.sleep(0.5)
    return None

def close_all():
    for _ in range(6):
        poss = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
        if not poss:
            return True
        for p in poss:
            t = mt5.symbol_info_tick(SYMBOL)
            buy = p.type == mt5.ORDER_TYPE_BUY
            mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL,
                            "position": p.ticket, "volume": p.volume,
                            "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                            "price": t.bid if buy else t.ask, "deviation": 200,
                            "magic": MAGIC, "type_filling": FILLING})
            time.sleep(0.15)
        time.sleep(1)
    return False

def probe(delay):
    calls, filled, fail = [], 0, None
    for i in range(N):
        typ = mt5.ORDER_TYPE_BUY if i % 2 == 0 else mt5.ORDER_TYPE_SELL
        r, ms = market(typ)
        c, ping = conn()
        code = r.retcode if r else None
        calls.append(ms)
        print(f"  #{i:02d} {ms:7.1f} ms code={code} connected={c} ping_us={ping}")
        if code != mt5.TRADE_RETCODE_DONE:
            fail = (i, code, mt5.last_error())
            break
        filled += 1
        if delay:
            time.sleep(delay)
    avg = sum(calls) / len(calls) if calls else 0
    rec = wait_connected() if fail else 0
    print(f"delay={delay*1000:.0f}ms filled={filled}/{N} avg_call={avg:.1f}ms "
          f"first_fail={fail} reconnect_s={rec}")
    print("  closed clean:", close_all())


def req_for(typ, px):
    return {"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": LOT,
            "type": typ, "price": px, "deviation": 200, "magic": MAGIC,
            "comment": "probe", "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": FILLING}

def send_req(req):
    r = mt5.order_send(req)
    return (r.retcode, r.price) if r else (None, 0.0)

def probe_parallel(workers, pairs=20):
    t = mt5.symbol_info_tick(SYMBOL)
    reqs = []
    for _ in range(pairs):
        reqs.append(req_for(mt5.ORDER_TYPE_BUY, t.ask))
        reqs.append(req_for(mt5.ORDER_TYPE_SELL, t.bid))
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(send_req, reqs))
    wall = (time.perf_counter() - t0) * 1000
    ok = [(rq, o) for rq, o in zip(reqs, out) if o[0] == mt5.TRADE_RETCODE_DONE]
    buys = [o[1] for rq, o in ok if rq["type"] == mt5.ORDER_TYPE_BUY]
    sells = [o[1] for rq, o in ok if rq["type"] == mt5.ORDER_TYPE_SELL]
    codes = {}
    for o in out:
        codes[o[0]] = codes.get(o[0], 0) + 1
    print(f"workers={workers} wall={wall:.0f}ms filled={len(ok)}/{len(reqs)} "
          f"buy={min(buys, default=0):.3f}-{max(buys, default=0):.3f} "
          f"sell={min(sells, default=0):.3f}-{max(sells, default=0):.3f} codes={codes}")
    if codes.get(10031):
        print("  reconnect_s:", wait_connected())
    print("  closed clean:", close_all())

if __name__ == "__main__":
    assert mt5.initialize(), mt5.last_error()
    mt5.symbol_select(SYMBOL, True)
    for w in (1, 2, 4, 8):
        probe_parallel(w)
        time.sleep(10)
    mt5.shutdown()