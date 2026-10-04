"""Bulk order opening/closing through the EA bridge (plan phase E).

open_position_batch is the async replacement for the strategy's per-order
sequential loop. positions_get is always the source of truth — the EA reply
is advisory only. If the batch ends up short after retries it ABORTS: close
everything opened for that batch and raise BulkOpenAborted (plan section 1).

Volume limits (user decision, overrides plan wording):
- MAX_CONFIGS.json MAX_LOT_PER_ASSET  = per-order split cap (matches
  GridBounceStrategyEngine._split_and_execute_orders semantics).
- MAX_CONFIGS.json MAX_VOLUME_PER_ASSET = per-symbol TOTAL volume limit.
  Preflight rejects the batch if total planned volume exceeds it and suggests
  position counts at 95% / 100% of the limit (UI guidance).
"""

import asyncio
import itertools
import logging
import secrets
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import MetaTrader5 as mt5

logger = logging.getLogger("ea")

MAGIC_NUMBER = 123456   # the strategy's existing magic

MAX_CONFIGS_PATH = Path(__file__).resolve().parent.parent / "MAX_CONFIGS.json"


def _load_max_configs() -> dict:
    with MAX_CONFIGS_PATH.open(encoding="utf-8") as f:
        return json.load(f)["maxConfigs"]


try:
    _MAX = _load_max_configs()
    MAX_LOT_PER_ASSET = _MAX["MAX_LOT_PER_ASSET"]
    MAX_VOLUME_PER_ASSET = _MAX["MAX_VOLUME_PER_ASSET"]
    MIN_STOP_PIPS_PER_ASSET = _MAX["MIN_STOP_PIPS_PER_ASSET"]
except Exception as e:   # pragma: no cover - fallback to known values
    logger.error(f"Could not load MAX_CONFIGS.json: {e}")
    from core.engine.grid_bounce_strategy_engine import (
        MAX_LOT_PER_ASSET, MIN_STOP_PIPS_PER_ASSET)
    MAX_VOLUME_PER_ASSET = {}


class BulkOpenAborted(RuntimeError):
    """Raised when the bulk open was still short after retries; the strategy
    must mark the cycle failed and not start it."""


class UseSequentialFallback(RuntimeError):
    """Signal for the strategy: use the existing sequential path this cycle."""


class LimitPreflightError(ValueError):
    """Preflight refused to arm the limit-trigger ladder (doc 08 section 5).
    Carries a specific message naming the offending value -- no silent
    fallback to burst mode."""


def _split_lot(lot: float, max_lot: float) -> List[float]:
    """Split a lot into chunks not exceeding max_lot."""
    if lot <= max_lot:
        return [float(lot)]
    chunks = []
    remaining = float(lot)
    while remaining > 0:
        chunk = min(remaining, max_lot)
        chunks.append(chunk)
        remaining -= chunk
    return chunks


def suggest_position_counts(symbol: str, lot: float) -> Optional[Tuple[int, int]]:
    """Position counts keeping total volume at 95% and 100% of the symbol's
    total volume limit (user's UI-guidance requirement). None if unknown."""
    limit = MAX_VOLUME_PER_ASSET.get(symbol)
    if not limit or lot <= 0:
        return None
    return (int(limit * 0.95 // lot), int(limit // lot))


def _check_total_volume(symbol: str, orders: List[dict]):
    """Reject the batch if total planned volume exceeds the symbol's total
    volume limit; include the 95%/100% suggestions in the message."""
    total_volume = sum(float(o["lot"]) for o in orders)
    limit = MAX_VOLUME_PER_ASSET.get(symbol)
    if limit is None or total_volume <= limit:
        return
    lot = float(orders[0]["lot"]) if orders else 0.0
    suggestion = ""
    counts = suggest_position_counts(symbol, lot)
    if counts:
        suggestion = (
            f" Reduce positions to {counts[0]} (95% of limit, total volume "
            f"{counts[0] * lot:.2f}) or {counts[1]} (100%, {counts[1] * lot:.2f}).")
    raise ValueError(
        f"preflight: total volume {total_volume:.2f} exceeds the "
        f"{symbol} volume limit {limit}.{suggestion}")


def _check_min_stops(symbol: str, side: str, tp: float, sl: float) -> Optional[str]:
    """Every moving-side TP/SL level must respect the minimum stop distance.
    Do NOT clamp — the strategy's levels are anchored on the start price.
    Returns an error string or None."""
    if tp == 0.0 and sl == 0.0:
        return None
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return "no symbol info/tick for stop-distance preflight"
    point = info.point or 1.0
    stop_pips = MIN_STOP_PIPS_PER_ASSET.get(symbol, 10)
    min_dist = max(stop_pips * point,
                   max(getattr(info, "trade_stops_level", 0), 10) * point)
    for name, price in (("TP", tp), ("SL", sl)):
        if price == 0.0:
            continue
        if side == "buy":
            ref = tick.bid
            ok_tp = price - ref >= min_dist
            ok_sl = ref - price >= min_dist
        else:
            ref = tick.ask
            ok_tp = ref - price >= min_dist
            ok_sl = price - ref >= min_dist
        if name == "TP" and not ok_tp:
            return f"{side} TP {price} within {min_dist} of ref {ref}"
        if name == "SL" and not ok_sl:
            return f"{side} SL {price} within {min_dist} of ref {ref}"
    return None


def preflight(symbol: str, orders: List[dict]) -> None:
    """Fail loudly BEFORE sending anything (plan E.2a)."""
    _check_total_volume(symbol, orders)

    # Free margin check via order_calc_margin
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    account = mt5.account_info()
    if tick is None or info is None or account is None:
        raise ValueError("preflight: no tick/symbol/account info")
    required = 0.0
    for o in orders:
        price = tick.ask if o["side"] == "B" else tick.bid
        m = mt5.order_calc_margin(
            mt5.ORDER_TYPE_BUY if o["side"] == "B" else mt5.ORDER_TYPE_SELL,
            symbol, float(o["lot"]), price)
        if m is None:
            raise ValueError(
                f"preflight: order_calc_margin failed: {mt5.last_error()}")
        required += m
    if required > account.margin_free:
        raise ValueError(
            f"preflight: required margin {required:.2f} exceeds free margin "
            f"{account.margin_free:.2f}")

    # Minimum stop distance for every moving-side level
    for o in orders:
        err = _check_min_stops(symbol, o["side"], float(o.get("tp", 0.0)),
                               float(o.get("sl", 0.0)))
        if err:
            raise ValueError(f"preflight: {err}")


def build_order_lines(symbol: str, moving_side: str, moving_lot: float,
                      constant_lot: float, moving_total: int, constant_total: int,
                      moving_tp_fn, moving_sl_fn,
                      grid_level_up: float, grid_level_down: float,
                      moving_freq: float) -> List[dict]:
    """Build the interleaved order list (plan E.2c).

    Tag = 4 random hex chars (batch id) + M/C + 3-digit slot index + optional
    split suffix, e.g. a3f1M007, a3f1C012b. Moving-side orders carry their
    precomputed TP/SL; constant-side orders carry tp=0, sl=0. Lots above
    MAX_LOT_PER_ASSET are split into several lines (suffixes a, b, ...).
    """
    batch_id = secrets.token_hex(2)   # 4 hex chars
    max_lot = MAX_LOT_PER_ASSET.get(symbol, 100)

    moving_side_letter = "B" if moving_side == "buy" else "S"
    constant_side_letter = "S" if moving_side == "buy" else "B"

    moving_lines: List[dict] = []
    for n in range(1, moving_total + 1):
        tp = moving_tp_fn(grid_level_up, grid_level_down, n, moving_freq, moving_side)
        sl = moving_sl_fn(grid_level_up, grid_level_down, n, moving_freq, moving_side)
        for ci, chunk in enumerate(_split_lot(moving_lot, max_lot)):
            suffix = chr(ord('a') + ci) if ci else ""
            moving_lines.append({
                "side": moving_side_letter, "lot": chunk, "tp": tp, "sl": sl,
                "tag": f"{batch_id}M{n:03d}{suffix}"[:31],
                "slot": n, "kind": "moving"})

    constant_lines: List[dict] = []
    for n in range(1, constant_total + 1):
        for ci, chunk in enumerate(_split_lot(constant_lot, max_lot)):
            suffix = chr(ord('a') + ci) if ci else ""
            constant_lines.append({
                "side": constant_side_letter, "lot": chunk, "tp": 0.0, "sl": 0.0,
                "tag": f"{batch_id}C{n:03d}{suffix}"[:31],
                "slot": n, "kind": "constant"})

    # Interleave buys and sells (plan E.2c, itertools.zip_longest pattern)
    lines: List[dict] = []
    for m, c in itertools.zip_longest(moving_lines, constant_lines):
        if m is not None:
            lines.append(m)
        if c is not None:
            lines.append(c)

    tags = [o["tag"] for o in lines]
    if len(set(tags)) != len(tags):
        raise ValueError("internal: duplicate tags generated for batch")
    return lines


async def open_position_batch(bridge, symbol: str,
                              orders: List[dict]) -> Dict[str, Tuple[int, float]]:
    """Open a batch through the EA and reconcile against positions_get.

    Returns {tag: (ticket, entry_price)}. Raises BulkOpenAborted if the batch
    could not be completed fully (everything opened for the batch is closed),
    or UseSequentialFallback if the EA is not healthy (never restart
    mid-session, plan E.2b).
    """
    preflight(symbol, orders)

    if not await bridge.healthy():
        raise UseSequentialFallback("EA not healthy at batch start")

    result = await bridge.open_batch(symbol, MAGIC_NUMBER, orders)
    logger.info(
        f"EA batch on {symbol}: {len(orders)} lines, sent_ok={result.get('sent_ok')} "
        f"replies_ok={result.get('replies_ok')} replies_fail={result.get('replies_fail')} "
        f"submit_ms={result.get('submit_ms')} last_reply_ms={result.get('last_reply_ms')}")

    def _snapshot():
        positions = mt5.positions_get(symbol=symbol) or []
        return {p.comment: (p.ticket, p.price_open) for p in positions
                if p.magic == MAGIC_NUMBER}

    tickets: Dict[str, Tuple[int, float]] = {}
    missing: List[str] = []
    for attempt in range(3):   # initial + max 2 retry passes
        await asyncio.sleep(0.2 if attempt == 0 else 0.5)
        snap = await asyncio.to_thread(_snapshot)
        tickets = {}
        missing = []
        for o in orders:
            tag = o["tag"]
            if tag in snap:
                tickets[tag] = snap[tag]
            else:
                missing.append(tag)
        if not missing:
            break
        if attempt < 2:
            logger.warning(
                f"bulk open: {len(missing)} order(s) absent from positions_get "
                f"(retry pass {attempt + 1}/2): {missing}")
            retry_orders = [o for o in orders if o["tag"] in missing]
            try:
                await bridge.open_batch(symbol, MAGIC_NUMBER, retry_orders)
            except Exception as e:
                logger.error(f"bulk open: retry pass failed: {e}")

    if missing:
        # ABORT: close everything opened for this batch (plan section 1)
        logger.error(
            f"bulk open ABORT on {symbol}: {len(missing)} order(s) never "
            f"filled after retries: {missing}")
        await _abort_cleanup(bridge, symbol)
        raise BulkOpenAborted(
            f"{len(missing)} of {len(orders)} orders not filled after retries; "
            f"batch closed, cycle not started")

    return tickets


# ===========================================================================
# Limit-trigger plan builder (doc 08 sections 5 and 8)
#
# Pure arithmetic: takes the config snapshot and a mid price, and produces both
# ladders and both contingent bursts. It never talks to the broker, so it can
# be eyeballed offline (tools/plan_check.py).
# ===========================================================================

# Which side of each ladder carries TP/SL follows the moving/constant setting,
# NOT which side triggered (doc 08 section 8 worked example).
def _carries_stops(role: str, moving_side: str) -> bool:
    """True when this role's orders are the MOVING side and must carry TP/SL."""
    if role in ("PB", "CB"):        # buy-side lines
        return moving_side == "buy"
    return moving_side == "sell"    # PS / CS are sell-side lines


def _scenario_stops(center, grid_distance, moving_freq, moving_side,
                    moving_tp_fn, moving_sl_fn, slot):
    """Moving-slot TP/SL anchored on this scenario's CENTER."""
    up = center + grid_distance
    down = center - grid_distance
    tp = moving_tp_fn(up, down, slot, moving_freq, moving_side)
    sl = moving_sl_fn(up, down, slot, moving_freq, moving_side)
    return float(tp), float(sl)


def compute_limit_levels(symbol, mid, entry_offset, info, tick):
    """Effective offset and both levels, with the stops/spread floor applied.

    floor = max(MIN_STOP_PIPS_PER_ASSET[symbol], trade_stops_level * point)
            + current spread

    Single floor for the whole system -- the same MIN_STOP_PIPS_PER_ASSET that
    _check_min_stops already uses. No separate safety_margin knob.
    """
    point = float(getattr(info, "point", 0) or 0)
    digits = int(getattr(info, "digits", 5))
    stops_level = int(getattr(info, "trade_stops_level", 0) or 0)
    spread = float(tick.ask - tick.bid)
    min_stop_pips = MIN_STOP_PIPS_PER_ASSET.get(symbol, 10)

    floor = max(float(min_stop_pips) * point, float(stops_level) * point) + spread
    requested = float(entry_offset)
    effective = max(requested, floor)
    clamped = effective > requested

    lower = round(mid - effective, digits)
    upper = round(mid + effective, digits)
    return {
        "requested_offset": requested,
        "effective_offset": effective,
        "floor": floor,
        "clamped": clamped,
        "spread": spread,
        "min_stop_pips": min_stop_pips,
        "stops_level": stops_level,
        "point": point,
        "digits": digits,
        "mid": mid,
        "lower": lower,
        "upper": upper,
    }


def build_limit_plan(symbol, moving_side, moving_lot, constant_lot,
                     moving_total, constant_total, moving_tp_fn, moving_sl_fn,
                     grid_distance, moving_freq, lower, upper,
                     cmd_hint=None):
    """Build every line the EA needs for one limit-trigger open.

    Returns a dict with both levels, the per-scenario centers and a `lines`
    list of PB/PS/CS/CB entries. Which lines carry TP/SL follows the
    moving/constant setting, so exactly one of {PB, CS} and exactly one of
    {PS, CB} carry stops.
    """
    batch = cmd_hint or secrets.token_hex(2)   # 4 hex chars
    max_lot = MAX_LOT_PER_ASSET.get(symbol, 100)
    lines = []

    def _emit(role, letter, lot, center, slot, split_idx):
        suffix = chr(ord('a') + split_idx) if split_idx else ""
        # Tag: 4 hex id + 2-char role + 3-digit slot + optional split letter.
        # Carries enough to map a ticket back to slot and role after a restart,
        # and stays well inside the 31-char MT5 comment limit.
        tag = f"{batch}{role}{slot:03d}{suffix}"[:31]
        if _carries_stops(role, moving_side):
            tp, sl = _scenario_stops(center, grid_distance, moving_freq,
                                     moving_side, moving_tp_fn, moving_sl_fn,
                                     slot)
        else:
            # Constant-side orders never carry stops (agent/04: a stray stop
            # here looks exactly like a queue bug on the terminal).
            tp, sl = 0.0, 0.0
        lines.append({
            "role": role, "side": letter, "lot": float(lot),
            "tp": tp, "sl": sl, "tag": tag, "slot": slot,
            "kind": "moving" if _carries_stops(role, moving_side) else "constant",
        })

    # --- lower scenario, center = lower -----------------------------------
    # PB buys at the lower level; CS market sells fire if the buys fill.
    buy_letter = "B" if moving_side == "buy" else "S"
    for n in range(1, moving_total + 1):
        for ci, chunk in enumerate(_split_lot(moving_lot, max_lot)):
            _emit("PB", "B", chunk, lower, n, ci)
    for n in range(1, constant_total + 1):
        for ci, chunk in enumerate(_split_lot(constant_lot, max_lot)):
            _emit("CS", "S", chunk, lower, n, ci)

    # --- upper scenario, center = upper -----------------------------------
    for n in range(1, moving_total + 1):
        for ci, chunk in enumerate(_split_lot(moving_lot, max_lot)):
            _emit("PS", "S", chunk, upper, n, ci)
    for n in range(1, constant_total + 1):
        for ci, chunk in enumerate(_split_lot(constant_lot, max_lot)):
            _emit("CB", "B", chunk, upper, n, ci)

    tags = [ln["tag"] for ln in lines]
    if len(set(tags)) != len(tags):
        raise ValueError("internal: duplicate tags generated for limit plan")

    pb = [l for l in lines if l["role"] == "PB"]
    ps = [l for l in lines if l["role"] == "PS"]
    cs = [l for l in lines if l["role"] == "CS"]
    cb = [l for l in lines if l["role"] == "CB"]
    return {
        "symbol": symbol,
        "lower": float(lower),
        "upper": float(upper),
        "center_lower": float(lower),
        "center_upper": float(upper),
        "moving_side": moving_side,
        "batch": batch,
        "lines": lines,
        "pb_count": len(pb), "ps_count": len(ps),
        "cs_count": len(cs), "cb_count": len(cb),
        "expected_buy": len(pb),
        "expected_sell": len(ps),
        "burst_expected": len(cs) + len(cb),
    }


def preflight_limit(symbol, plan, levels, burst_mode="AFTER_CANCEL"):
    """Doc 08 section 5 preflight. Raises LimitPreflightError with a specific
    message; never silently falls back to burst mode."""
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    account = mt5.account_info()
    if info is None or tick is None or account is None:
        raise LimitPreflightError(
            f"preflight: no symbol info/tick/account info for {symbol}")

    point = levels["point"]
    stops_level = float(levels["stops_level"]) * point
    freeze_level = float(getattr(info, "trade_freeze_level", 0) or 0) * point

    # 1. Pending-order cap, counting EVERY pending on the account (the cap is
    #    account-wide, so another symbol's armed ladder counts against it).
    cap = int(getattr(account, "limit_orders", 0) or 0)
    if cap > 0:
        existing = len(mt5.orders_get() or ())
        need = plan["pb_count"] + plan["ps_count"]
        if existing + need > cap:
            raise LimitPreflightError(
                f"preflight: account pending-order cap is {cap}; {existing} "
                f"already open and this arm needs {need} more "
                f"({plan['pb_count']} buy limits + {plan['ps_count']} sell "
                f"limits). Reduce counts on {symbol} or on other symbols.")

    # 2. Both levels must respect the stops level against a fresh tick.
    for name, level, ref in (("lower", plan["lower"], tick.ask),
                             ("upper", plan["upper"], tick.bid)):
        if abs(ref - level) < stops_level:
            raise LimitPreflightError(
                f"preflight: {name} level {level} is within {stops_level} of "
                f"the current {ref} on {symbol} (stops level). Price moved "
                f"since the plan was built.")

    # 3. Freeze level on both ladders.
    for name, level, ref in (("lower", plan["lower"], tick.ask),
                             ("upper", plan["upper"], tick.bid)):
        if freeze_level > 0 and abs(ref - level) < freeze_level:
            raise LimitPreflightError(
                f"preflight: {name} level {level} is inside the freeze level "
                f"({freeze_level}) around {ref} on {symbol}")

    # 4. Per-direction volume against the symbol limit (pendings count).
    limit = MAX_VOLUME_PER_ASSET.get(symbol)
    if limit:
        buy_vol = sum(l["lot"] for l in plan["lines"] if l["role"] == "PB")
        sell_vol = sum(l["lot"] for l in plan["lines"] if l["role"] == "PS")
        for side, vol in (("buy ladder", buy_vol), ("sell ladder", sell_vol)):
            if vol > limit:
                raise LimitPreflightError(
                    f"preflight: {side} volume {vol} exceeds the {symbol} "
                    f"limit {limit}. Reduce counts or lot size.")
        if burst_mode == "PARALLEL":
            # Both contingent bursts sit on top of the still-live pendings.
            burst_vol = sum(l["lot"] for l in plan["lines"]
                            if l["role"] in ("CS", "CB"))
            if buy_vol + sell_vol + burst_vol > limit:
                raise LimitPreflightError(
                    f"preflight: PARALLEL mode needs pendings ({buy_vol + sell_vol}) "
                    f"plus burst ({burst_vol}) = {buy_vol + sell_vol + burst_vol}, "
                    f"over the {symbol} limit {limit}. Use AFTER_CANCEL or "
                    f"reduce counts.")

    # 5. Per-order volume after splitting.
    max_lot = MAX_LOT_PER_ASSET.get(symbol, 100)
    for l in plan["lines"]:
        if l["lot"] > max_lot + 1e-9:
            raise LimitPreflightError(
                f"preflight: order lot {l['lot']} exceeds the per-order cap "
                f"{max_lot} for {symbol} after splitting (tag {l['tag']})")

    # 6. Validate stops for the pending ladders against their pending entry
    #    levels. Contingent CS/CB orders are not sent until a trigger occurs,
    #    so checking their future stops against today's market price rejects
    #    valid plans when the market is still between the two trigger levels.
    min_dist = max(
        float(MIN_STOP_PIPS_PER_ASSET.get(symbol, 10)) * point,
        max(float(getattr(info, "trade_stops_level", 0) or 0), 10.0) * point,
    )
    for l in plan["lines"]:
        if l["role"] not in ("PB", "PS") or (l["tp"] == 0.0 and l["sl"] == 0.0):
            continue
        entry = plan["lower"] if l["role"] == "PB" else plan["upper"]
        if l["side"] == "B":
            if l["tp"] and l["tp"] - entry < min_dist:
                raise LimitPreflightError(
                    f"preflight: buy TP {l['tp']} within {min_dist} of "
                    f"pending entry {entry} ({symbol} {l['role']})")
            if l["sl"] and entry - l["sl"] < min_dist:
                raise LimitPreflightError(
                    f"preflight: buy SL {l['sl']} within {min_dist} of "
                    f"pending entry {entry} ({symbol} {l['role']})")
        else:
            if l["tp"] and entry - l["tp"] < min_dist:
                raise LimitPreflightError(
                    f"preflight: sell TP {l['tp']} within {min_dist} of "
                    f"pending entry {entry} ({symbol} {l['role']})")
            if l["sl"] and l["sl"] - entry < min_dist:
                raise LimitPreflightError(
                    f"preflight: sell SL {l['sl']} within {min_dist} of "
                    f"pending entry {entry} ({symbol} {l['role']})")

    return True


async def _abort_cleanup(bridge, symbol: str):
    """close_all via the EA, verify zero left via positions_get, retry up to
    3 times, then fall back to sequential close for stragglers (plan E.2f)."""
    def _remaining():
        return [p.ticket for p in (mt5.positions_get(symbol=symbol) or [])
                if p.magic == MAGIC_NUMBER]

    for attempt in range(3):
        try:
            await bridge.close_all(symbol, MAGIC_NUMBER)
        except Exception as e:
            logger.error(f"abort cleanup close_all failed: {e}")
        await asyncio.sleep(0.5)
        if not await asyncio.to_thread(_remaining):
            logger.info("abort cleanup: all batch positions verified closed")
            return
    # Sequential fallback for stragglers (plan E.2f)
    from core.engine.queued_close_strategy_engine import (
        QueuedCloseStrategyEngine)
    dummy = QueuedCloseStrategyEngine.__new__(QueuedCloseStrategyEngine)
    dummy.symbol = symbol
    for ticket in await asyncio.to_thread(_remaining):
        try:
            if not dummy._close_position(ticket):
                logger.error(
                    f"abort cleanup: sequential close of #{ticket} failed")
        except Exception as e:
            logger.error(f"abort cleanup: sequential close of #{ticket} raised: {e}")
