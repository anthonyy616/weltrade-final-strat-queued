import asyncio
import MetaTrader5 as mt5
from core.ea_bridge import EABridge

SYMBOL, MAGIC = "FX Vol 20", 999004

async def main():
    b = EABridge()
    print("ping:", await b.ping())

    orders = []
    for i in range(5):
        orders.append({"side": "B", "lot": 0.01, "tag": f"t0B{i:03d}"})
        orders.append({"side": "S", "lot": 0.01, "tag": f"t0S{i:03d}"})

    r = await b.open_batch(SYMBOL, MAGIC, orders)
    print({k: r.get(k) for k in ("sent_ok", "replies_ok", "submit_ms", "last_reply_ms")}, r["fails"])

    mt5.initialize()
    pos = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
    print("open:", len(pos), "comments:", sorted(p.comment for p in pos))

    r = await b.close_all(SYMBOL, MAGIC)
    print("close:", r.get("replies_ok"), r["fails"])
    mt5.shutdown()

asyncio.run(main())