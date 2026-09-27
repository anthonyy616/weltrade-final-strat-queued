"""Queued Close Strategy Engine.

A pool of positions opens at startup; the rest of the cycle only ever closes
them down in a queued order. No bouncing, no nuclear reset on a single TP/SL.
Spec: agent/02-architecture.md (authoritative for names/shapes/formulas).
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import MetaTrader5 as mt5_module

from core.engine.activity_logger import ActivityLogger
from core.persistence.repository import Repository

logger = logging.getLogger("queued_close")
mt5: Any = mt5_module

# ---------------------------------------------------------------------------
# Pure closing-target math (agent/02 §6). Raw price units — pip values are
# added to prices directly, matching the existing fork's convention.
# Do not re-derive these formulas; unit-test against the worked example below.
#
# Worked example (center=1000, grid_distance=50, moving_freq=10,
# constant_freq=8, diff=2):
#   Moving=BUY:  up TP 1060/1070/1080... down SL 940/930/920...
#                constant up 1058/1068/1078... constant down 938/928/918...
#   Moving=SELL: down TP 940/930/920... up SL 1060/1070/1080...
#                constant down 942/932/922... constant up 1062/1072/1082...
# ---------------------------------------------------------------------------


def moving_tp_level(grid_level_up: float, grid_level_down: float, n: int,
                    moving_freq: float, moving_side: str) -> float:
    """TP level for the moving side, slot n (1-based)."""
    if moving_side == "buy":
        return grid_level_up + n * moving_freq
    return grid_level_down - n * moving_freq


def moving_sl_level(grid_level_up: float, grid_level_down: float, n: int,
                    moving_freq: float, moving_side: str) -> float:
    """SL level for the moving side, slot n (1-based)."""
    if moving_side == "buy":
        return grid_level_down - n * moving_freq
    return grid_level_up + n * moving_freq


def constant_target(moving_level: float, diff: float, moving_side: str) -> float:
    """Constant-side target paired with a moving level.

    Constant targets always sit `diff` BEHIND the moving level that just fired
    — on the side already reached — never ahead of it.
    """
    if moving_side == "buy":
        return moving_level - diff
    return moving_level + diff


def compute_constant_targets(grid_level_up: float, grid_level_down: float,
                             moving_freq: float, constant_freq: float,
                             moving_side: str, constant_total: int):
    """Precompute both direction target lists in full.

    Returns (up_targets, down_targets) as lists of (price, slot_index) tuples,
    each `constant_total` long, 1-based slot indices. Two separate lists with
    no shared index space — never merge them (agent/04 coding rules).
    """
    diff = moving_freq - constant_freq
    up_targets = []
    down_targets = []
    for n in range(1, constant_total + 1):
        up_level = moving_sl_level(grid_level_up, grid_level_down, n,
                                   moving_freq, moving_side) \
            if moving_side == "sell" \
            else moving_tp_level(grid_level_up, grid_level_down, n,
                                 moving_freq, moving_side)
        down_level = moving_tp_level(grid_level_up, grid_level_down, n,
                                     moving_freq, moving_side) \
            if moving_side == "sell" \
            else moving_sl_level(grid_level_up, grid_level_down, n,
                                 moving_freq, moving_side)
        up_targets.append((constant_target(up_level, diff, moving_side), n))
        down_targets.append((constant_target(down_level, diff, moving_side), n))
    return up_targets, down_targets


# ---------------------------------------------------------------------------
# State model (agent/02 §4) — replaces GridLevel/StrategyState entirely.
# ---------------------------------------------------------------------------


@dataclass
class MovingPositionRecord:
    ticket: int
    entry: float
    tp_price: float
    sl_price: float
    direction: str          # "buy" or "sell"
    slot_index: int         # 1-based index into its TP-direction list
    closed: bool = False
    lot: float = 0.01       # additive vs spec: needed for PnL display from deals


@dataclass
class ConstantTargetLevel:
    price: float
    direction: str          # "up" or "down" — which grid direction this belongs to
    slot_index: int         # 1-based
    fired: bool = False     # True once released and successfully closed


@dataclass
class QueuedClose:
    target: ConstantTargetLevel
    enqueued_at: float      # timestamp, for logging/debugging only
    retry_count: int = 0


@dataclass
class QueuedCloseState:
    phase: str = "IDLE"              # IDLE, ACTIVE, RESETTING
    center_price: float = 0.0
    grid_level_up: float = 0.0
    grid_level_down: float = 0.0

    # Config snapshot — captured ONCE at cycle start (agent/05: never read
    # moving_freq/constant_freq/grid_distance from live config after start()).
    moving_side: str = "buy"         # "buy" or "sell"; constant is the other
    moving_freq: float = 0.0
    constant_freq: float = 0.0
    grid_distance: float = 0.0

    moving_positions: Dict[int, MovingPositionRecord] = field(default_factory=dict)
    constant_tickets: List[int] = field(default_factory=list)   # open constant-side tickets

    up_targets: List[ConstantTargetLevel] = field(default_factory=list)
    down_targets: List[ConstantTargetLevel] = field(default_factory=list)

    moving_total: int = 0
    constant_total: int = 0
    moving_closed_count: int = 0
    constant_closed_count: int = 0

    close_queue: List[QueuedClose] = field(default_factory=list)

    catching_up: bool = False   # True during reconnect reconciliation

    cycle_count: int = 0
    realized_pnl: float = 0.0

    # Runtime-only (not persisted, additive vs spec): pool lot sizes captured
    # at start() alongside the other snapshot values.
    moving_lot: float = 0.01
    constant_lot: float = 0.01


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class QueuedCloseStrategyEngine:
    """Queued Close Strategy: open the full pool at startup, close down via a
    queue released by moving-side TP/SL fires. One instance per symbol.
    """

    MAGIC_NUMBER = 123456

    def __init__(self, config_manager, symbol: str, user_id: str = "default",
                 session_logger=None):
        self.config_manager = config_manager
        self.symbol = symbol
        self.user_id = user_id
        self.session_logger = session_logger

        self.state = QueuedCloseState()
        self.running = False
        self.graceful_stop = False

        self.execution_lock = asyncio.Lock()
        self.activity_log = ActivityLogger(symbol, user_id, session_logger)
        self.repository: Optional[Repository] = None

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def config(self) -> Dict[str, Any]:
        return self.config_manager.get_symbol_config(self.symbol) or {}

    @property
    def mt5_symbol(self) -> str:
        """Resolve broker symbol. Weltrade: no suffix (agent/02 §2).
        Always go through this property in broker calls, never self.symbol.
        """
        return self.symbol

    @property
    def current_price(self) -> float:
        tick = mt5.symbol_info_tick(self.mt5_symbol)
        if tick:
            return (tick.ask + tick.bid) / 2
        return self.state.center_price

    async def _ensure_repository_async(self) -> Repository:
        if self.repository is None:
            self.repository = Repository(self.symbol)
            await self.repository.initialize()
        return self.repository

    async def start_ticker(self):
        """Compatibility hook for orchestrator config refreshes."""
        return None

    # ------------------------------------------------------------------
    # Startup (agent/02 §5, agent/03 startup pseudocode)
    # ------------------------------------------------------------------

    async def start(self):
        """Open the full pool and enter ACTIVE phase. Config values are
        snapshotted exactly once here; nothing downstream re-reads them."""
        if self.running:
            return

        await self._ensure_repository_async()

        cfg = self.config
        # --- Config snapshot: the ONLY place these are read from config ---
        constant_side = cfg.get("constant_side", "sell")
        if constant_side not in ("buy", "sell"):
            self.activity_log.log_error(
                f"Invalid constant_side '{constant_side}' — refusing to start"
            )
            return
        moving_side = "sell" if constant_side == "buy" else "buy"

        self.state.moving_side = moving_side
        self.state.moving_freq = float(cfg.get("moving_freq", 10.0))
        self.state.constant_freq = float(cfg.get("constant_freq", 8.0))
        self.state.grid_distance = float(cfg.get("grid_distance", 50.0))

        buy_count = max(1, int(cfg.get("buy_count", 5)))
        sell_count = max(1, int(cfg.get("sell_count", 5)))
        buy_lot = max(0.01, float(cfg.get("buy_lot", 0.01)))
        sell_lot = max(0.01, float(cfg.get("sell_lot", 0.01)))

        self.state.moving_total = buy_count if moving_side == "buy" else sell_count
        self.state.constant_total = buy_count if constant_side == "buy" else sell_count
        self.state.moving_lot = buy_lot if moving_side == "buy" else sell_lot
        self.state.constant_lot = buy_lot if constant_side == "buy" else sell_lot

        self.running = True
        self.graceful_stop = False
        self._clear_cycle_state()
        self.state.cycle_count += 1

        await self._start_new_cycle_at_market()

    def _clear_cycle_state(self):
        """Reset per-cycle in-memory lists (agent/05: never let stale entries
        accumulate across cycles)."""
        self.state.moving_positions.clear()
        self.state.constant_tickets = []
        self.state.up_targets = []
        self.state.down_targets = []
        self.state.close_queue = []
        self.state.moving_closed_count = 0
        self.state.constant_closed_count = 0
        self.state.catching_up = False

    async def _start_new_cycle_at_market(self):
        """Compute grid levels, precompute targets, open the full pool."""
        self.state.phase = "RESETTING"

        tick = mt5.symbol_info_tick(self.mt5_symbol)
        if not tick:
            self.activity_log.log_error("Failed to get tick for cycle start")
            self.running = False
            self.state.phase = "IDLE"
            return

        center = (tick.ask + tick.bid) / 2
        self.state.center_price = center
        self.state.grid_level_up = center + self.state.grid_distance
        self.state.grid_level_down = center - self.state.grid_distance

        # Precompute both direction target lists in full (two separate lists —
        # never merge them, agent/04).
        up_prices, down_prices = compute_constant_targets(
            self.state.grid_level_up, self.state.grid_level_down,
            self.state.moving_freq, self.state.constant_freq,
            self.state.moving_side, self.state.constant_total,
        )
        self.state.up_targets = [
            ConstantTargetLevel(price=p, direction="up", slot_index=n)
            for p, n in up_prices
        ]
        self.state.down_targets = [
            ConstantTargetLevel(price=p, direction="down", slot_index=n)
            for p, n in down_prices
        ]

        repo = await self._ensure_repository_async()
        self.activity_log.log_start(self.state.cycle_count, center)
        self.activity_log.log_info(
            f"Cycle #{self.state.cycle_count}: moving={self.state.moving_side.upper()} "
            f"x{self.state.moving_total} @ {self.state.moving_lot}, "
            f"constant={('sell' if self.state.moving_side == 'buy' else 'buy').upper()} "
            f"x{self.state.constant_total} @ {self.state.constant_lot}, "
            f"grid={self.state.grid_distance}, mf={self.state.moving_freq}, "
            f"cf={self.state.constant_freq}"
        )

        # Persist precomputed targets before opening anything
        await repo.save_constant_targets(self.state.cycle_count, [
            {"direction": t.direction, "slot_index": t.slot_index,
             "price": t.price, "fired": False}
            for t in self.state.up_targets + self.state.down_targets
        ])

        moving_leg = "MovingBuy" if self.state.moving_side == "buy" else "MovingSell"
        constant_leg = "ConstantBuy" if self.state.moving_side == "buy" else "ConstantSell"

        # Open all moving-side positions, each with its TP/SL slot (1-based, no
        # two positions share a slot).
        for n in range(1, self.state.moving_total + 1):
            tp = moving_tp_level(self.state.grid_level_up, self.state.grid_level_down,
                                 n, self.state.moving_freq, self.state.moving_side)
            sl = moving_sl_level(self.state.grid_level_up, self.state.grid_level_down,
                                 n, self.state.moving_freq, self.state.moving_side)
            ticket, entry = await self._open_market_order(
                self.state.moving_side, self.state.moving_lot, moving_leg,
                tp_price=tp, sl_price=sl)
            if not ticket:
                self.activity_log.log_error(
                    f"Moving slot {n}: order failed — pool opened partially")
                continue
            self.state.moving_positions[ticket] = MovingPositionRecord(
                ticket=ticket, entry=entry, tp_price=tp, sl_price=sl,
                direction=self.state.moving_side, slot_index=n,
                lot=self.state.moving_lot)
            await repo.save_moving_position(
                ticket, self.state.cycle_count, entry, tp, sl,
                self.state.moving_side, n, closed=False)
            self.activity_log.log_fire(
                self.state.cycle_count, moving_leg, entry, self.state.moving_lot,
                tp, sl, ticket)

        # Open all constant-side positions with NO TP/SL whatsoever. The
        # order request below deliberately never carries sl/tp fields for
        # these — do not "helpfully" add default stops here (agent/04).
        constant_side = "sell" if self.state.moving_side == "buy" else "buy"
        for _ in range(self.state.constant_total):
            ticket, entry = await self._open_market_order(
                constant_side, self.state.constant_lot, constant_leg,
                tp_price=None, sl_price=None)
            if not ticket:
                self.activity_log.log_error(
                    "Constant position order failed — pool opened partially")
                continue
            self.state.constant_tickets.append(ticket)
            await repo.save_constant_ticket(ticket, self.state.cycle_count)
            self.activity_log.log_fire(
                self.state.cycle_count, constant_leg, entry,
                self.state.constant_lot, 0.0, 0.0, ticket)

        self.state.phase = "ACTIVE"
        await self._save_symbol_state()

    # ------------------------------------------------------------------
    # Broker interaction (patterns reused from GridBounceStrategyEngine)
    # ------------------------------------------------------------------

    async def _open_market_order(self, direction: str, lot_size: float,
                                 leg_name: str,
                                 tp_price: Optional[float] = None,
                                 sl_price: Optional[float] = None) -> Tuple[int, float]:
        """Send a market order. Returns (ticket, entry_price); (0, 0.0) on failure.

        tp_price/sl_price: absolute prices computed from the closing-target
        formulas. Pass None for constant-side positions — no sl/tp fields are
        sent in that case, ever.
        """
        sym = self.mt5_symbol
        tick = mt5.symbol_info_tick(sym)
        if not tick:
            self.activity_log.log_error(f"No tick for {leg_name}")
            return 0, 0.0

        if direction == "buy":
            exec_price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
        else:
            exec_price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL

        # Snapshot existing tickets: on some account modes result.order does
        # not match the eventual position ticket — match by exclusion.
        positions_before = mt5.positions_get(symbol=sym)
        existing_tickets = set(p.ticket for p in positions_before) if positions_before else set()

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(lot_size),
            "type": order_type,
            "price": exec_price,
            "magic": self.MAGIC_NUMBER,
            "comment": f"{leg_name} C{self.state.cycle_count}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "deviation": 200,
        }
        # Only moving-side orders carry stops. Constant-side requests must
        # never include sl/tp (agent/03: an accidental stop here looks exactly
        # like a queue bug on the terminal).
        if tp_price is not None:
            request["tp"] = float(tp_price)
        if sl_price is not None:
            request["sl"] = float(sl_price)

        result = mt5.order_send(request)
        if result is None:
            self.activity_log.log_error(f"{leg_name} order failed: {mt5.last_error()}")
            return 0, 0.0
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.activity_log.log_error(
                f"{leg_name} order failed: {result.comment if result else 'unknown'}")
            return 0, 0.0

        # Position-confirmation pause (existing codebase pattern)
        await asyncio.sleep(0.1)

        positions_after = mt5.positions_get(symbol=sym)
        actual_ticket = result.order
        actual_entry = exec_price
        if positions_after:
            for pos in positions_after:
                if pos.ticket not in existing_tickets:
                    actual_ticket = pos.ticket
                    actual_entry = pos.price_open
                    break
            else:
                for pos in positions_after:
                    if pos.ticket == result.order:
                        actual_ticket = pos.ticket
                        actual_entry = pos.price_open
                        break
        return actual_ticket, actual_entry

    def _close_position(self, ticket: int) -> bool:
        """Close a single MT5 position at market. Returns success."""
        sym = self.mt5_symbol
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False
        pos = positions[0]
        tick = mt5.symbol_info_tick(sym)
        if not tick:
            return False

        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            close_price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            close_price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": close_price,
            "deviation": 50,
            "magic": self.MAGIC_NUMBER,
            "comment": "queued-close",
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    async def _force_close_everything(self):
        """Close every open strategy position on this symbol immediately
        (cycle-end remainder, terminate-all, exhausted-queue reset)."""
        positions = mt5.positions_get(symbol=self.mt5_symbol) or []
        closed = 0
        for pos in positions:
            if pos.magic != self.MAGIC_NUMBER:
                continue
            if self._close_position(pos.ticket):
                closed += 1
        self.activity_log.log_info(f"Force-closed {closed} positions")

    # ------------------------------------------------------------------
    # Lifecycle controls
    # ------------------------------------------------------------------

    async def stop(self):
        """Graceful stop — the in-progress cycle finishes naturally, then the
        bot stops. No new cycle starts once this flag is set."""
        if not self.running:
            return
        self.graceful_stop = True
        self.activity_log.log_graceful_stop(self.state.cycle_count, "manual/timeout")
        if self.state.phase == "IDLE" or (
            not self.state.moving_positions and not self.state.constant_tickets
        ):
            self.running = False
            self.activity_log.log_stop(self.state.cycle_count, "graceful_stop_immediate")

    async def terminate(self):
        """User-initiated terminate: close everything immediately, clear queue
        and counts as part of the SAME operation (agent/05: a stale queue
        surviving terminate causes a false start next cycle)."""
        self.activity_log.log_info("TERMINATE: Closing all positions...")
        await self._force_close_everything()
        self.state.close_queue = []
        self.state.catching_up = False

        repo = self.repository
        if repo is not None:
            await repo.clear_constant_queue()
            await repo.clear_constant_tickets()
            await repo.clear_moving_positions()

        self._clear_cycle_state()
        self.running = False
        self.graceful_stop = False
        self.state.phase = "IDLE"
        self.state.cycle_count = 0
        await self._save_symbol_state()
        self.activity_log.log_info("TERMINATE: complete")

    async def close(self):
        """Release persistent resources."""
        if self.repository is not None:
            await self.repository.close()
            self.repository = None

    # ------------------------------------------------------------------
    # Status API
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        moving_open = sum(1 for r in self.state.moving_positions.values() if not r.closed)
        constant_open = len(self.state.constant_tickets)
        return {
            "running": self.running,
            "phase": self.state.phase,
            "cycle_count": self.state.cycle_count,
            "center_price": self.state.center_price,
            "open_positions": moving_open + constant_open,
            "moving_total": self.state.moving_total,
            "constant_total": self.state.constant_total,
            "moving_closed": self.state.moving_closed_count,
            "constant_closed": self.state.constant_closed_count,
            "moving_open": moving_open,
            "constant_open": constant_open,
            "queue_length": len(self.state.close_queue),
            "catching_up": self.state.catching_up,
            "realized_pnl": self.state.realized_pnl,
            "graceful_stop": self.graceful_stop,
            "is_resetting": self.state.phase == "RESETTING",
            "step": self.state.cycle_count,
            "iteration": self.state.cycle_count,
            "current_price": self.current_price,
        }

    # ------------------------------------------------------------------
    # Tick handling (agent/02 §7, agent/03 tick pseudocode)
    # ------------------------------------------------------------------

    async def on_external_tick(self, tick_data: dict):
        """Called by orchestrator on every tick. All strategy logic for one
        tick — closure detection, queue release, and cycle-end check — runs
        inside ONE continuous execution_lock block (agent/05)."""
        ask = tick_data.get('ask', 0.0)
        bid = tick_data.get('bid', 0.0)
        if not self.running or ask <= 0 or bid <= 0:
            return

        self._last_ask = ask
        self._last_bid = bid

        async with self.execution_lock:
            # catching_up check is INSIDE the lock, atomic with reconciliation
            # (agent/05: checking outside the lock leaves a race window).
            if self.state.catching_up:
                return

            if self.state.phase != "ACTIVE":
                return

            await self._process_closures_and_queue(ask, bid)
            await self._check_cycle_end(ask, bid)

            # Graceful stop: cycle finished and pool drained — stop now
            if self.graceful_stop and self.state.phase == "IDLE":
                self.running = False
                self.activity_log.log_stop(
                    self.state.cycle_count, "graceful_stop_complete")

    async def _process_closures_and_queue(self, ask: float, bid: float):
        """Detect moving closures, release queue entries, drain the queue.
        Must be called with execution_lock held."""
        # --- 1. Detect moving-position closures: diff tracked vs live ---
        positions = mt5.positions_get(symbol=self.mt5_symbol)
        live_tickets = set(p.ticket for p in positions) if positions else set()

        closed_tickets = [
            t for t, rec in self.state.moving_positions.items()
            if not rec.closed and t not in live_tickets
        ]
        # Process in slot order if multiple closed in the same tick
        closed_tickets.sort(key=lambda t: self.state.moving_positions[t].slot_index)

        for ticket in closed_tickets:
            await self._handle_moving_closure(ticket, live_tickets)

        # --- 2. Drain the queue: one close attempt per pending item ---
        if self.state.close_queue:
            await self._process_close_queue()

    async def _handle_moving_closure(self, ticket: int, live_tickets: set):
        """Process one moving closure and release its constant queue entry.
        Also used by reconciliation replay — keep this the single closure path
        (agent/03: never write a parallel release implementation)."""
        rec = self.state.moving_positions.get(ticket)
        if rec is None or rec.closed:
            return

        rec.closed = True
        self.state.moving_closed_count += 1

        # Determine TP vs SL: prefer the broker's own deal record (exact),
        # fall back to proximity inference if history is unavailable.
        close_price, hit_type = self._extract_close_info(ticket)
        if close_price > 0:
            is_tp = (hit_type == "tp")
        else:
            ask, bid = self._last_ask, self._last_bid
            check = bid if rec.direction == "buy" else ask
            is_tp = abs(check - rec.tp_price) <= abs(check - rec.sl_price)
            close_price = rec.tp_price if is_tp else rec.sl_price
        close_price = rec.tp_price if is_tp else rec.sl_price
        if rec.direction == "buy":
            realized = (close_price - rec.entry) * rec.lot
        else:
            realized = (rec.entry - close_price) * rec.lot
        self.state.realized_pnl += realized

        if is_tp:
            self.activity_log.log_tp_hit(
                ticket, "MovingBuy" if rec.direction == "buy" else "MovingSell",
                close_price, realized, triggered_reset=False)
        else:
            self.activity_log.log_sl_hit(
                ticket, "MovingBuy" if rec.direction == "buy" else "MovingSell",
                close_price, realized, triggered_reset=False)

        if self.repository is not None:
            await self.repository.mark_moving_position_closed(ticket)

        # Release the paired constant target, keyed by direction+slot.
        # Direction comes from the closure's own slot semantics: for moving=BUY
        # a TP fire is an up-direction closure, an SL fire is down; for
        # moving=SELL the reverse. Lookup is keyed by direction — reversal
        # handling is implicit, no special branch (agent/03).
        if is_tp:
            direction = "up" if rec.direction == "buy" else "down"
        else:
            direction = "down" if rec.direction == "buy" else "up"
        await self._enqueue_constant_target(direction, rec.slot_index, released_by=ticket)

    async def _enqueue_constant_target(self, direction: str, slot_index: int,
                                       released_by: int):
        """Enqueue the constant target for (direction, slot) if not already
        fired or already pending."""
        targets = self.state.up_targets if direction == "up" else self.state.down_targets
        target = next((t for t in targets if t.slot_index == slot_index), None)
        if target is None:
            self.activity_log.log_error(
                f"No {direction} target for slot {slot_index} (released by moving #{released_by})")
            return
        if target.fired:
            return
        if any(q.target is target for q in self.state.close_queue):
            return

        self.state.close_queue.append(
            QueuedClose(target=target, enqueued_at=time.time()))
        if self.repository is not None:
            await self.repository.enqueue_constant_close(
                self.state.cycle_count, direction, slot_index, 0, time.time())
        self.activity_log.log_info(
            f"Queued constant target {direction}#{slot_index} @ {target.price} "
            f"(released by moving #{released_by})")

    async def _process_close_queue(self):
        """Attempt one constant close per pending queue item. On failure,
        requeue with retry_count incremented; if the failing item is the last
        pending one, terminate all and restart the cycle (agent/02 §8).
        Iterate a snapshot — the real list mutates during the pass."""
        for item in list(self.state.close_queue):
            if not self.state.constant_tickets:
                self.activity_log.log_error(
                    "Queue pending but no open constant tickets remain — clearing queue")
                self.state.close_queue = []
                if self.repository is not None:
                    await self.repository.clear_constant_queue(self.state.cycle_count)
                return

            # FIFO: any open constant ticket satisfies any released entry
            # (running-count model — no slot binding, agent/04).
            ticket = self.state.constant_tickets[0]
            success = self._close_position(ticket)

            if success:
                item.target.fired = True
                self.state.constant_closed_count += 1
                self.state.constant_tickets.pop(0)
                self.state.close_queue.remove(item)

                if self.repository is not None:
                    await self.repository.delete_constant_ticket(ticket)
                    await self.repository.delete_constant_queue_entry(
                        self.state.cycle_count, item.target.direction, item.target.slot_index)
                    await self.repository.mark_constant_target_fired(
                        self.state.cycle_count, item.target.direction, item.target.slot_index)

                constant_leg = ("ConstantBuy" if self.state.moving_side == "sell"
                                else "ConstantSell")
                self.activity_log.log_info(
                    f"{constant_leg} closed #{self.state.constant_closed_count}/"
                    f"{self.state.constant_total} (target {item.target.direction}"
                    f"#{item.target.slot_index} @ {item.target.price}, "
                    f"ticket {ticket})")
            else:
                item.retry_count += 1
                if self.repository is not None:
                    await self.repository.bump_constant_queue_retry(
                        self.state.cycle_count, item.target.direction,
                        item.target.slot_index, item.retry_count)
                self.activity_log.log_error(
                    f"Constant close failed for {item.target.direction}"
                    f"#{item.target.slot_index} — retry {item.retry_count}")
                # Terminate-and-restart only when this is the last pending item
                if len(self.state.close_queue) == 1:
                    self.activity_log.log_info(
                        "Last pending queue item failing — terminating all and restarting cycle")
                    await self._end_cycle(
                        "TERMINATE_QUEUE_EXHAUSTED", self._last_ask, self._last_bid)
                    return
                # otherwise: leave it, retry next tick (one attempt per tick)

    async def _check_cycle_end(self, ask: float, bid: float):
        """Cycle ends the instant either side reaches its configured closed
        count. Force-close the remainder immediately, queue included."""
        if self.state.phase != "ACTIVE":
            return
        if (self.state.moving_closed_count < self.state.moving_total
                and self.state.constant_closed_count < self.state.constant_total):
            return

        early = (self.state.moving_closed_count != self.state.moving_total) or (
            self.state.constant_closed_count != self.state.constant_total)
        await self._end_cycle(
            "EARLY_FORCE_CLOSE" if early else "NORMAL", ask, bid)

    async def _end_cycle(self, reason: str, ask: float, bid: float):
        """Force-close remainder, clear queue (DB rows too — agent/04), log
        completion type, then start a new cycle at current price."""
        self.state.phase = "RESETTING"

        await self._force_close_everything()

        # Explicitly clear this cycle's queue rows — never rely on the next
        # cycle's writes to make old rows irrelevant (agent/04).
        repo = self.repository
        if repo is not None:
            await repo.clear_constant_queue(self.state.cycle_count)
            await repo.clear_constant_tickets()
            await repo.clear_moving_positions()

        self.activity_log.log_cycle_complete(
            self.state.cycle_count, reason, self.state.realized_pnl,
            self.state.moving_closed_count, self.state.constant_closed_count)

        if self.graceful_stop:
            self.state.phase = "IDLE"
            self.running = False
            self.activity_log.log_stop(
                self.state.cycle_count, "graceful_stop_complete")
            await self._save_symbol_state()
            return

        # New cycle starts immediately at the current market price, same
        # configuration (config snapshot values are reused, not re-read).
        self._clear_cycle_state()
        self.state.cycle_count += 1
        await self._start_new_cycle_at_market()

    # Last known prices for closure inference helpers
    _last_ask: float = 0.0
    _last_bid: float = 0.0

    async def _save_symbol_state(self):
        """Persist phase/center/cycle via the repository's symbol_state row."""
        repo = self.repository
        if repo is None:
            return
        metadata = json.dumps({
            "strategy": "queued_close",
            "moving_side": self.state.moving_side,
            "moving_freq": self.state.moving_freq,
            "constant_freq": self.state.constant_freq,
            "grid_distance": self.state.grid_distance,
            "moving_total": self.state.moving_total,
            "constant_total": self.state.constant_total,
            "moving_closed_count": self.state.moving_closed_count,
            "constant_closed_count": self.state.constant_closed_count,
            "realized_pnl": self.state.realized_pnl,
            "moving_lot": self.state.moving_lot,
            "constant_lot": self.state.constant_lot,
        })
        await repo.save_state(
            phase=self.state.phase,
            center_price=self.state.center_price,
            iteration=self.state.cycle_count,
            cycle_id=self.state.cycle_count,
            anchor_price=self.state.center_price,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Reconciliation on reconnect (agent/02 §10, agent/03 pseudocode)
    # ------------------------------------------------------------------

    async def reconcile_on_startup(self) -> dict:
        """Diff live MT5 positions against DB state, replay every closure that
        happened while disconnected through the SAME handler a live tick uses,
        then resume. Runs under execution_lock with catching_up held True for
        the entire pass (agent/05: no live tick may interleave mid-replay)."""
        await self._ensure_repository_async()

        summary = {"replayed": [], "orphans": [], "cycle_restarted": False}

        async with self.execution_lock:
            self.state.catching_up = True
            try:
                state_row = await self.repository.get_state()
                if state_row and state_row.get("phase") in ("ACTIVE", "RESETTING"):
                    await self._rebuild_state_from_db(state_row)
                    await self._replay_missed_closures(summary)
                else:
                    # No active cycle persisted — clean slate
                    self.state.phase = "IDLE"
                    self.state.catching_up = False
                    return summary
            finally:
                self.state.catching_up = False

        self.activity_log.log_info(f"[RECOVERY] {summary}")
        return summary

    async def _rebuild_state_from_db(self, state_row: dict):
        """Restore in-memory cycle state from persisted tables."""
        metadata = {}
        if state_row.get("metadata"):
            try:
                metadata = json.loads(state_row["metadata"])
            except Exception:
                metadata = {}

        self.state.phase = state_row.get("phase", "IDLE")
        self.state.cycle_count = int(state_row.get("cycle_id", 0) or 0)
        self.state.center_price = float(state_row.get("center_price", 0.0) or 0.0)
        self.state.realized_pnl = float(metadata.get("realized_pnl", 0.0) or 0.0)
        self.state.moving_side = metadata.get("moving_side", "buy")
        self.state.moving_freq = float(metadata.get("moving_freq", 0.0))
        self.state.constant_freq = float(metadata.get("constant_freq", 0.0))
        self.state.grid_distance = float(metadata.get("grid_distance", 0.0))
        self.state.moving_total = int(metadata.get("moving_total", 0) or 0)
        self.state.constant_total = int(metadata.get("constant_total", 0) or 0)
        self.state.moving_closed_count = int(metadata.get("moving_closed_count", 0) or 0)
        self.state.constant_closed_count = int(metadata.get("constant_closed_count", 0) or 0)
        self.state.moving_lot = float(metadata.get("moving_lot", 0.01) or 0.01)
        self.state.constant_lot = float(metadata.get("constant_lot", 0.01) or 0.01)
        self.state.grid_level_up = self.state.center_price + self.state.grid_distance
        self.state.grid_level_down = self.state.center_price - self.state.grid_distance

        # Rebuild target lists from DB (fired flags included)
        self.state.up_targets = []
        self.state.down_targets = []
        for row in await self.repository.get_constant_targets(self.state.cycle_count):
            t = ConstantTargetLevel(
                price=float(row["price"]), direction=row["direction"],
                slot_index=int(row["slot_index"]), fired=bool(row["fired"]))
            (self.state.up_targets if t.direction == "up" else self.state.down_targets).append(t)

        # Rebuild moving positions (open ones only — closed rows are history)
        self.state.moving_positions = {}
        for row in await self.repository.get_moving_positions(open_only=True):
            self.state.moving_positions[int(row["ticket"])] = MovingPositionRecord(
                ticket=int(row["ticket"]), entry=float(row["entry"]),
                tp_price=float(row["tp_price"]), sl_price=float(row["sl_price"]),
                direction=row["direction"], slot_index=int(row["slot_index"]),
                closed=False, lot=self.state.moving_lot)

        self.state.constant_tickets = await self.repository.get_constant_tickets()

        # Rebuild pending queue from DB
        self.state.close_queue = []
        for row in await self.repository.get_constant_queue():
            direction = row["direction"]
            targets = self.state.up_targets if direction == "up" else self.state.down_targets
            target = next(
                (t for t in targets if t.slot_index == int(row["slot_index"])), None)
            if target is not None and not target.fired:
                self.state.close_queue.append(QueuedClose(
                    target=target, enqueued_at=float(row["enqueued_at"] or 0.0),
                    retry_count=int(row["retry_count"] or 0)))

    async def _replay_missed_closures(self, summary: dict):
        """Find DB-tracked open tickets missing from MT5, pull their deal
        history, and replay each closure through _handle_moving_closure — the
        exact function a live tick uses."""
        positions = mt5.positions_get(symbol=self.mt5_symbol)
        live_tickets = set(p.ticket for p in positions) if positions else set()

        tracked_open = set(self.state.moving_positions.keys())
        missing = tracked_open - live_tickets

        for ticket in sorted(missing):
            rec = self.state.moving_positions.get(ticket)
            if rec is None:
                continue
            close_price, hit_type = self._extract_close_info(ticket)
            summary["replayed"].append(ticket)
            self.activity_log.log_info(
                f"[RECOVERY] Replaying closure of moving #{ticket} "
                f"({'TP' if hit_type == 'tp' else 'SL'} @ {close_price})")
            # Same closure path as a live tick — no parallel implementation
            await self._handle_moving_closure(ticket, live_tickets)

        # After replay: if the cycle had already ended during the outage, the
        # normal cycle-end check handles it — no special "recovered" branch.
        if (self.state.moving_closed_count >= self.state.moving_total
                or self.state.constant_closed_count >= self.state.constant_total):
            summary["cycle_restarted"] = True
            await self._end_cycle(
                "EARLY_FORCE_CLOSE"
                if (self.state.moving_closed_count != self.state.moving_total
                    or self.state.constant_closed_count != self.state.constant_total)
                else "NORMAL",
                self._last_ask, self._last_bid)

    def _extract_close_info(self, ticket: int) -> Tuple[float, str]:
        """Pull the closing (OUT) deal for a position. A position can have
        multiple deal rows (in/out/partial) — filter for the OUT deal
        specifically rather than taking the first row (agent/05)."""
        try:
            deals = mt5.history_deals_get(position=ticket)
            if not deals:
                return 0.0, "sl"
            out_deals = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
            if not out_deals:
                return 0.0, "sl"
            deal = out_deals[-1]  # last OUT deal = the actual close
            reason = getattr(deal, "reason", None)
            hit = "tp" if reason == mt5.DEAL_REASON_TP else "sl"
            return float(deal.price), hit
        except Exception as e:
            self.activity_log.log_error(f"Deal history lookup failed for #{ticket}: {e}")
            return 0.0, "sl"
