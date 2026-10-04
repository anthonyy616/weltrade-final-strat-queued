"""EA doctor CLI (plan phase F.1): diagnose the WTExecutor EA end-to-end.

Usage:
    python tools/ea_doctor.py             # paths, install, compile, ping
    python tools/ea_doctor.py --relaunch  # also exercise the restart path
    python tools/ea_doctor.py --smoke     # open 5+5 demo orders, verify tags
                                          # round-trip via comments, close them
DEMO ACCOUNT ONLY — --smoke places real orders.
"""

import asyncio
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5   # noqa: E402

from core.ea_bridge import EABridge   # noqa: E402
from core import ea_provisioner as prov   # noqa: E402
from core import bulk_orders as bo   # noqa: E402

from dotenv import load_dotenv
load_dotenv()

SYMBOL = os.getenv("EA_CHART_SYMBOL", "FX Vol 20")
MAGIC = 999004   # test magic, NOT the strategy's


def section(title: str):
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


async def main():
    do_relaunch = "--relaunch" in sys.argv
    do_smoke = "--smoke" in sys.argv
    bridge = EABridge()

    section("Resolved paths")
    print(f"EA source      : {prov.EA_SOURCE} (v{prov.WT_EA_VERSION})")
    print(f"source exists  : {prov.EA_SOURCE.exists()}")
    try:
        folder = bridge.resolve_common_files()
        print(f"common files   : {folder}")
    except Exception as e:
        print(f"common files   : UNRESOLVED ({e})")

    section("Terminal")
    ti = mt5.terminal_info()
    if ti is None:
        print("initialising MT5 ...")
        if not mt5.initialize():
            print(f"MT5 initialize failed: {mt5.last_error()}")
            return
        ti = mt5.terminal_info()
    print(f"path           : {ti.path}")
    print(f"data_path      : {ti.data_path}")
    print(f"commondata_path: {ti.commondata_path}")
    print(f"portable flag  : {Path(ti.data_path) == Path(ti.path) if ti.data_path and ti.path else 'unknown'}")
    ai = mt5.account_info()
    if ai:
        print(f"account login  : {ai.login} server={ai.server}")
        print(f"positions open : {mt5.positions_total()}")

    section("Provision (install + compile + ping)")
    status = await prov.ensure_ea_ready(
        bridge, allow_restart=do_relaunch)
    if status.available:
        print(f"EA available, version {status.version}")
    else:
        print(f"EA UNAVAILABLE: {status.reason}")

    section("Ping")
    print(f"healthy()      : {await bridge.healthy()}")

    if do_smoke:
        section("Smoke: 5 buys + 5 sells, comment round-trip")
        orders = [{"side": "B", "lot": 0.01, "tp": 0.0, "sl": 0.0,
                   "tag": f"smkeB{i:03d}"} for i in range(5)] + \
                 [{"side": "S", "lot": 0.01, "tp": 0.0, "sl": 0.0,
                   "tag": f"smkeS{i:03d}"} for i in range(5)]
        res = await bridge.open_batch(SYMBOL, MAGIC, orders)
        print(f"open: sent_ok={res.get('sent_ok')} replies_ok={res.get('replies_ok')} "
              f"submit_ms={res.get('submit_ms')} last_reply_ms={res.get('last_reply_ms')} "
              f"fails={res.get('fails')}")
        import time as _t
        _t.sleep(1.0)
        positions = mt5.positions_get(symbol=SYMBOL) or []
        mine = [p for p in positions if p.magic == MAGIC]
        comments = sorted(p.comment for p in mine)
        expected = sorted(o["tag"] for o in orders)
        print(f"positions_get  : {len(mine)} positions")
        if comments == expected:
            print("comment round-trip: OK — comments match tags exactly")
        else:
            print("comment round-trip: MISMATCH")
            print(f"  expected: {expected}")
            print(f"  actual  : {comments}")
        close = await bridge.close_all(SYMBOL, MAGIC)
        print(f"close: replies_ok={close.get('replies_ok')} fails={close.get('fails')}")
        _t.sleep(1.0)
        left = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
        print(f"remaining after close_all: {len(left)}")

    mt5.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
