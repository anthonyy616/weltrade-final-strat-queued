"""Queued Close Strategy Engine.

A pool of positions opens at startup; the rest of the cycle only ever closes
them down in a queued order. No bouncing, no nuclear reset on a single TP/SL.
Spec: agent/02-architecture.md (authoritative for names/shapes/formulas).
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import MetaTrader5 as mt5_module

from core.engine.activity_logger import ActivityLogger
from core.persistence.repository import Repository

logger = logging.getLogger("queued_close")
mt5: Any = mt5_module

# Bulk-order path (plan phase E): available = None means "not provisioned yet"
# (sequential fallback); otherwise the server's ea_status dict is consulted at
# cycle start and again at batch time via bridge.healthy().
try:
    from core import bulk_orders
    from core.ea_bridge import EABridge
    _bulk_available = True
except Exception as _e:   # pragma: no cover
    logger.warning(f"bulk order path unavailable: {_e}")
    _bulk_available = False

# Per-order lot cap, shared with the bulk path so the sequential fallback
# enforces the same limit the EA path does.
MAX_LOT_PER_ASSET = getattr(bulk_orders, "MAX_LOT_PER_ASSET", {}) if _bulk_available else {}

# How many times one cycle may re-arm around a new price before the open is
# treated as a failure (doc 08 section 8: ARM_TIMEOUT is not an error, but an
# unbounded re-arm loop would hang the open forever).
MAX_LIMIT_REARMS = 3

# Reconcile after DONE retries for ~1 s: positions_get can lag the deal events
# that the EA has already seen (doc 08 section 8).
RECONCILE_RETRY_S = 1.0
RECONCILE_RETRY_DELAY_S = 0.2


def _split_lot(lot: float, max_lot: float) -> List[float]:
    """Split a lot into chunks not exceeding the per-order cap (up to 20)."""
    if lot <= max_lot:
        return [float(lot)]
    chunks: List[float] = []
    remaining = float(lot)
    while remaining > 1e-9 and len(chunks) < 20:
        chunk = min(remaining, float(max_lot))
        chunks.append(chunk)
        remaining -= chunk
    return chunks

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

    # Limit-trigger open mode snapshot (doc 08 section 4/8). Captured at
    # start() with the rest of the snapshot, never read live mid-cycle.
    open_mode: str = "burst"
    entry_offset: float = 50.0
    armed_timeout_seconds: int = 120
    win_fill_deadline_ms: int = 1500
    cancel_ack_deadline_ms: int = 1500
    burst_mode: str = "AFTER_CANCEL"
    max_consecutive_open_failures: int = 3

    # Set only while the limit open is in flight, so stop()/terminate() can
    # tell an armed symbol from an open one. Persisted with the ARMED phase.
    arm_lower: float = 0.0
    arm_upper: float = 0.0
    arm_cmd_id: str = ""


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

        # EA bulk-open support (plan phase E). Both are injected by the server
        # at startup; if absent, the engine always uses the sequential path.
        self.ea_bridge = None
        self.ea_status: Optional[dict] = None

        # Set by reconcile_on_startup when a live cycle was recovered from
        # disk. start() resumes that cycle instead of opening a second pool.
        self.recovered = False

        # Consecutive failed limit opens. In memory only on purpose: a restart
        # clears it, and persisted counts would be stale shadow state.
        self._consecutive_open_failures = 0
        # True only between "persisted ARMED" and the terminal phase.
        self._limit_armed = False
        self._last_open_quality: Optional[dict] = None

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
        snapshotted exactly once here; nothing downstream re-reads them.

        If startup reconciliation already recovered a live cycle for this
        symbol, this is a no-op: the recovered cycle is already running and
        its config snapshot came from the DB, not from the live config. Only
        a genuinely fresh start reads config and opens a pool.
        """
        if self.running:
            return

        await self._ensure_repository_async()

        if self.recovered and self.state.phase == "ACTIVE":
            self.running = True
            self.graceful_stop = False
            self.activity_log.log_info(
                f"Resuming recovered cycle #{self.state.cycle_count} "
                f"({len(self.state.moving_positions)} moving / "
                f"{len(self.state.constant_tickets)} constant open) "
                "— no new pool opened"
            )
            return

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

        # Limit-trigger snapshot, read here and never again for this cycle.
        self.state.open_mode = cfg.get("open_mode", "burst")
        self.state.entry_offset = float(cfg.get("entry_offset", 50.0))
        gbl = self.config_manager.get_global_config() or {}
        self.state.armed_timeout_seconds = int(gbl.get("armed_timeout_seconds", 120))
        self.state.win_fill_deadline_ms = int(gbl.get("win_fill_deadline_ms", 1500))
        self.state.cancel_ack_deadline_ms = int(gbl.get("cancel_ack_deadline_ms", 1500))
        self.state.burst_mode = gbl.get("burst_mode", "AFTER_CANCEL")
        self.state.max_consecutive_open_failures = int(
            gbl.get("max_consecutive_open_failures", 3))

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
        constant_leg = "ConstantBuy" if self.state.moving_side == "sell" else "ConstantSell"

        # --- Open path selection (doc 08 section 8) ---
        #
        # limit_trigger takes its own branch and NEVER falls back to the
        # sequential loop below: if the EA is unavailable or refuses, the open
        # fails loudly instead of silently opening in burst mode. Burst is
        # untouched: the EA bulk path first, sequential loop as its fallback.
        if self.state.open_mode == "limit_trigger":
            await self._try_limit_open(moving_leg, constant_leg)
            # _try_limit_open returns True when the open was HANDLED, which
            # includes a definitive stop by the failure breaker. Never stamp
            # ACTIVE over a symbol that was just stopped.
            if self.running and self.state.phase != "IDLE":
                self.state.phase = "ACTIVE"
                await self._save_symbol_state()
            return

        # --- Try the EA bulk path first (plan E.2/E.4); the sequential loop
        # below is the untouched fallback, selected when EA status is
        # unavailable or healthy() fails at cycle start. ---
        if await self._try_bulk_open(moving_leg, constant_leg):
            self.state.phase = "ACTIVE"
            await self._save_symbol_state()
            return

        # Open all moving-side positions, each with its TP/SL slot (1-based, no
        # two positions share a slot). Lots above MAX_LOT_PER_ASSET are split
        # into several orders carrying the SAME slot level; the first ticket
        # owns the slot (mirrors the EA bulk path's slot bookkeeping).
        max_lot = MAX_LOT_PER_ASSET.get(self.mt5_symbol, 100)
        for n in range(1, self.state.moving_total + 1):
            tp = moving_tp_level(self.state.grid_level_up, self.state.grid_level_down,
                                 n, self.state.moving_freq, self.state.moving_side)
            sl = moving_sl_level(self.state.grid_level_up, self.state.grid_level_down,
                                 n, self.state.moving_freq, self.state.moving_side)
            for chunk in _split_lot(self.state.moving_lot, max_lot):
                ticket, entry = await self._open_market_order(
                    self.state.moving_side, chunk, moving_leg,
                    tp_price=tp, sl_price=sl)
                if not ticket:
                    self.activity_log.log_error(
                        f"Moving slot {n}: order failed — pool opened partially")
                    continue
                self.state.moving_positions[ticket] = MovingPositionRecord(
                    ticket=ticket, entry=entry, tp_price=tp, sl_price=sl,
                    direction=self.state.moving_side, slot_index=n,
                    lot=chunk)
                await repo.save_moving_position(
                    ticket, self.state.cycle_count, entry, tp, sl,
                    self.state.moving_side, n, closed=False)
                self.activity_log.log_fire(
                    self.state.cycle_count, moving_leg, entry, chunk,
                    tp, sl, ticket)

        # Open all constant-side positions with NO TP/SL whatsoever. The
        # order request below deliberately never carries sl/tp fields for
        # these — do not "helpfully" add default stops here (agent/04).
        constant_side = "sell" if self.state.moving_side == "buy" else "buy"
        for _ in range(self.state.constant_total):
            for chunk in _split_lot(self.state.constant_lot, max_lot):
                ticket, entry = await self._open_market_order(
                    constant_side, chunk, constant_leg,
                    tp_price=None, sl_price=None)
                if not ticket:
                    self.activity_log.log_error(
                        "Constant position order failed — pool opened partially")
                    continue
                self.state.constant_tickets.append(ticket)
                await repo.save_constant_ticket(ticket, self.state.cycle_count)
                self.activity_log.log_fire(
                    self.state.cycle_count, constant_leg, entry,
                    chunk, 0.0, 0.0, ticket)

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
            invalid_stops = getattr(mt5, "TRADE_RETCODE_INVALID_STOPS", 10016)
            if result.retcode == invalid_stops and tp_price is not None:
                result = self._retry_on_invalid_stops(request, direction, leg_name)
                if result is None:
                    return 0, 0.0
            else:
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

    def _retry_on_invalid_stops(self, request: dict, direction: str, leg_name: str):
        """Retry an order the broker rejected for invalid stops.

        Unlike the legacy grid-bounce engine, this does NOT substitute a generic
        pip-distance stop: in this strategy the TP/SL levels ARE the closing
        targets, so rewriting them would silently corrupt the cycle's target
        math. Instead we re-send the SAME levels against a fresh tick — the
        usual cause is price moving between level computation and send.
        Returns the successful result, or None if it still fails.
        """
        sym = self.mt5_symbol
        fresh = mt5.symbol_info_tick(sym)
        if fresh is None:
            self.activity_log.log_error(
                f"{leg_name}: invalid stops and no fresh tick available for retry")
            return None

        retry = dict(request)
        retry["price"] = float(fresh.ask if direction == "buy" else fresh.bid)
        res = mt5.order_send(retry)
        if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
            self.activity_log.log_info(
                f"{leg_name}: retried at a fresh tick and filled "
                f"({retry['price']}); TP/SL unchanged")
            return res

        # Second failure: surface the real rejection so the operator can widen
        # grid_distance or lower moving_freq rather than guess.
        err = res.comment if res is not None else mt5.last_error()
        self.activity_log.log_error(
            f"{leg_name} order failed on invalid-stops retry: {err}")
        return None

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

    # ------------------------------------------------------------------
    # EA bulk path (plan phase E) — additive; the sequential path remains
    # the fallback and the final verifier.
    # ------------------------------------------------------------------

    async def _try_bulk_open(self, moving_leg: str, constant_leg: str) -> bool:
        """Open the whole pool through the EA in one batch. Returns True when
        the cycle start was handled via the EA path (success OR hard abort);
        False to use the sequential fallback."""
        if not _bulk_available or self.ea_bridge is None or self.ea_status is None:
            return False
        if not self.ea_status.get("available"):
            self.activity_log.log_info(
                f"EA unavailable: {self.ea_status.get('reason')} "
                "(sequential fallback active)")
            return False

        try:
            orders = bulk_orders.build_order_lines(
                self.mt5_symbol, self.state.moving_side,
                self.state.moving_lot, self.state.constant_lot,
                self.state.moving_total, self.state.constant_total,
                moving_tp_level, moving_sl_level,
                self.state.grid_level_up, self.state.grid_level_down,
                self.state.moving_freq)
        except ValueError as e:
            # Preflight-grade config error: fail the cycle loudly, open nothing
            self.activity_log.log_error(f"Bulk open preflight failed: {e}")
            self.state.phase = "IDLE"
            self.running = False
            return True

        try:
            tickets = await bulk_orders.open_position_batch(
                self.ea_bridge, self.mt5_symbol, orders)
        except bulk_orders.UseSequentialFallback as e:
            self.activity_log.log_error(
                f"EA not answering at cycle start ({e}) — sequential fallback")
            return False
        except bulk_orders.BulkOpenAborted as e:
            # Plan section 1: cycle failed — mark it, do not start it
            self.activity_log.log_error(f"BULK OPEN ABORTED: {e}")
            self.state.phase = "IDLE"
            self.running = False
            return True

        # Feed the tag -> (ticket, entry) map into the existing tracking via the
        # shared post-open routine (doc 08 section 8).
        await self._register_open_positions(orders, tickets, moving_leg,
                                           constant_leg)
        self.activity_log.log_info(
            f"EA bulk open: {len(tickets)} positions opened in one batch")
        return True

    def _record_open_quality(self, tickets: Dict[str, Tuple[int, float]],
                             mode: str, phase: Optional[dict] = None,
                             trigger_side: str = "n/a") -> None:
        """Append one open_quality.csv row for this open, in BOTH modes.

        Called from the shared post-open routine so burst and limit_trigger
        are measured the same way and the comparison is like-for-like. Reads
        the just-opened positions back from the broker rather than reusing
        the in-memory entries, because the metrics are about what the broker
        actually filled.

        Additive to burst mode and fully defensive: the only effect on an
        existing burst open is one extra read-only positions_get after the
        open has already completed, and any failure is logged and dropped.
        """
        try:
            from core import open_quality
            info = mt5.symbol_info(self.mt5_symbol)
            digits = int(getattr(info, "digits", 5)) if info else 5
            filled = [p for p in
                      (mt5.positions_get(symbol=self.mt5_symbol) or [])
                      if p.magic == self.MAGIC_NUMBER
                      and p.ticket in {t for t, _ in tickets.values()}]
            open_quality.record_open(
                self.user_id, self.mt5_symbol, mode, filled, digits=digits,
                phase=phase, trigger_side=trigger_side,
                entry_offset=(self.state.entry_offset
                              if mode == "limit_trigger" else None),
                log=self.activity_log)
        except Exception as e:
            # Metrics must never break the open that just succeeded.
            self.activity_log.log_error(
                f"[QUALITY] open_quality.csv row skipped: {e}")

    async def _register_open_positions(self, orders: List[dict],
                                       tickets: Dict[str, Tuple[int, float]],
                                       moving_leg: str, constant_leg: str,
                                       quality_mode: str = "burst",
                                       quality_phase: Optional[dict] = None,
                                       quality_side: str = "n/a"):
        """Shared post-open routine (doc 08 section 8).

        Registers opened positions into the engine's existing tracking,
        persists them and logs them. The burst branch and the limit branch both
        call this, so there is exactly ONE copy of this logic.

        `orders` entries carry "tag", "slot", "kind" and "lot"; `tickets` maps
        tag -> (ticket, entry) as verified against positions_get.

        The quality_* arguments are metrics-only and default to burst with no
        EA stamps, which is exactly what a burst open has.
        """
        repo = await self._ensure_repository_async()
        by_tag = {o["tag"]: o for o in orders}
        for tag, (ticket, entry) in tickets.items():
            order = by_tag.get(tag)
            if order is None:
                continue
            if order["kind"] == "moving":
                if order["slot"] in [r.slot_index for r in
                                     self.state.moving_positions.values()]:
                    continue   # split part already recorded for this slot
                tp = moving_tp_level(self.state.grid_level_up,
                                     self.state.grid_level_down,
                                     order["slot"], self.state.moving_freq,
                                     self.state.moving_side)
                sl = moving_sl_level(self.state.grid_level_up,
                                     self.state.grid_level_down,
                                     order["slot"], self.state.moving_freq,
                                     self.state.moving_side)
                self.state.moving_positions[ticket] = MovingPositionRecord(
                    ticket=ticket, entry=entry, tp_price=tp, sl_price=sl,
                    direction=self.state.moving_side,
                    slot_index=order["slot"], lot=order["lot"])
                await repo.save_moving_position(
                    ticket, self.state.cycle_count, entry, tp, sl,
                    self.state.moving_side, order["slot"], closed=False)
                self.activity_log.log_fire(
                    self.state.cycle_count, moving_leg, entry, order["lot"],
                    tp, sl, ticket)
            else:
                if ticket not in self.state.constant_tickets:
                    self.state.constant_tickets.append(ticket)
                    await repo.save_constant_ticket(ticket, self.state.cycle_count)
                    self.activity_log.log_fire(
                        self.state.cycle_count, constant_leg, entry,
                        order["lot"], 0.0, 0.0, ticket)

        # One metrics row per successful open, in either mode (doc 08 §8).
        self._record_open_quality(tickets, quality_mode, quality_phase,
                                  quality_side)

    # ------------------------------------------------------------------
    # Limit-trigger open path (doc 08 sections 5, 8 and 9)
    # ------------------------------------------------------------------

    async def _sweep_pendings(self, reason: str = "") -> bool:
        """Remove every pending order for this symbol's magic. EA command
        first, direct orders_get fallback inside the bridge (doc 08 section 8).

        Scoped to symbol+magic: the magic is shared across symbols, so a
        magic-wide sweep here would cancel another symbol's live ladder.
        """
        if self.ea_bridge is None:
            # No bridge: fall back to a direct sweep so a stop or terminate can
            # never leave a pending order behind.
            import MetaTrader5 as mt5_direct

            def _pending():
                return [o.ticket for o in
                        (mt5_direct.orders_get(symbol=self.mt5_symbol) or ())
                        if o.magic == self.MAGIC_NUMBER]

            for _ in range(3):
                pending = await asyncio.to_thread(_pending)
                if not pending:
                    return True
                await asyncio.to_thread(self._remove_pending_direct, pending)
                await asyncio.sleep(0.2)
            left = await asyncio.to_thread(_pending)
            self.activity_log.log_error(
                f"[LIMIT] direct pending sweep left {len(left)} order(s) "
                f"on {self.mt5_symbol} ({reason})")
            return False
        try:
            return await self.ea_bridge.cancel_all_pendings(
                self.mt5_symbol, self.MAGIC_NUMBER, deadline_s=10.0)
        except Exception as e:
            self.activity_log.log_error(
                f"[LIMIT] pending sweep raised on {self.mt5_symbol}: {e}")
            return False

    def _remove_pending_direct(self, tickets: List[int]) -> None:
        for tkt in tickets:
            try:
                mt5.order_send({
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "symbol": self.mt5_symbol,
                    "position": tkt,
                    "magic": self.MAGIC_NUMBER,
                    "comment": "eng-sweep",
                })
            except Exception as e:
                self.activity_log.log_error(
                    f"[LIMIT] direct remove of #{tkt} raised: {e}")

    async def _flatten_and_reset(self, reason: str):
        """Every limit failure converges here: sweep pendings, flatten, and
        leave the cycle ready to restart (doc 08 section 3.5)."""
        self.activity_log.log_info(f"[LIMIT] failure path ({reason}): "
                                   "sweeping pendings and flattening")
        await self._sweep_pendings(reason)
        await self._force_close_everything()
        repo = self.repository
        if repo is not None:
            await repo.clear_constant_queue(self.state.cycle_count)
            await repo.clear_constant_tickets()
            await repo.clear_moving_positions()
        self._clear_cycle_state()

    async def _try_limit_open(self, moving_leg: str, constant_leg: str) -> bool:
        """Arm a limit-trigger ladder, reconcile, then hand off to the shared
        post-open routine.

        Owns the retry loop so a failure never recurses: each iteration is a
        full restart of the cycle, bounded by max_consecutive_open_failures.
        Returns True whenever the open was handled (success, or a definitive
        stop) so the caller never falls through to the sequential path.
        """
        rearms = 0
        while True:
            status, detail = await self._limit_open_once(moving_leg,
                                                          constant_leg)

            if status == "OK":
                self._consecutive_open_failures = 0
                return True

            if status == "REARM" and rearms < MAX_LIMIT_REARMS:
                rearms += 1
                self.activity_log.log_info(
                    f"[LIMIT] re-arm {rearms}/{MAX_LIMIT_REARMS} around the "
                    f"current price ({detail})")
                # A re-arm is NOT a new cycle: same cycle id, same targets.
                # Only the levels move, so recompute them and try again.
                await self._recompute_cycle_levels()
                continue

            if status == "REARM":
                detail = (f"REARM_EXHAUSTED after {MAX_LIMIT_REARMS} re-arms "
                          f"({detail})")

            # Definitive failure: sweep, flatten, count it, restart or stop.
            await self._flatten_and_reset(detail)
            self._consecutive_open_failures += 1
            n = self._consecutive_open_failures
            limit = max(1, self.state.max_consecutive_open_failures)
            if n >= limit:
                self.state.phase = "IDLE"
                self.running = False
                self.state.arm_cmd_id = ""
                self.state.arm_lower = 0.0
                self.state.arm_upper = 0.0
                await self._save_symbol_state()
                self.activity_log.log_error(
                    f"[LIMIT] STOPPED: {n} consecutive failed opens on "
                    f"{self.mt5_symbol} (limit {limit}). Last reason: "
                    f"{detail}. Not retrying — widen entry_offset, lower the "
                    f"counts, or switch this symbol back to Burst mode.")
                return True

            self.activity_log.log_error(
                f"[LIMIT] open failed ({detail}); failure {n}/{limit}, "
                "restarting the cycle")
            # Restart the cycle from scratch (doc 08 section 8). The restart
            # re-enters _start_new_cycle_at_market, which re-enters THIS
            # method, so return instead of looping again -- otherwise the
            # outer frame arms a second time after the breaker has already
            # stopped the symbol.
            self._clear_cycle_state()
            self.state.cycle_count += 1
            await self._start_new_cycle_at_market()
            return True

    async def _recompute_cycle_levels(self, center: float = None):
        """Re-derive the cycle levels and target lists from a center.

        Targets are a pure function of the center (doc 08 section 8), so this
        is the single place that recomputes them. `center` is passed
        explicitly on a successful open -- the center is the level that
        TRIGGERED, never the average fill and never the live mid. With no
        argument (a re-arm) it re-anchors on the current mid, which is exactly
        what a re-arm is for.
        """
        if center is None:
            tick = mt5.symbol_info_tick(self.mt5_symbol)
            if not tick:
                return
            center = (tick.ask + tick.bid) / 2
        self.state.center_price = center
        self.state.grid_level_up = center + self.state.grid_distance
        self.state.grid_level_down = center - self.state.grid_distance
        up_prices, down_prices = compute_constant_targets(
            self.state.grid_level_up, self.state.grid_level_down,
            self.state.moving_freq, self.state.constant_freq,
            self.state.moving_side, self.state.constant_total)
        self.state.up_targets = [
            ConstantTargetLevel(price=p, direction="up", slot_index=n)
            for p, n in up_prices]
        self.state.down_targets = [
            ConstantTargetLevel(price=p, direction="down", slot_index=n)
            for p, n in down_prices]
        repo = await self._ensure_repository_async()
        await repo.save_constant_targets(self.state.cycle_count, [
            {"direction": t.direction, "slot_index": t.slot_index,
             "price": t.price, "fired": False}
            for t in self.state.up_targets + self.state.down_targets])

    def _log_limit_phase(self, d: dict) -> None:
        """One readable [LIMIT] line per EA phase, with the counts and timings
        that phase actually reported (doc 08 section 8).

        The EA's own journal already has the raw line; this is the copy that
        lands in the per-symbol activity log next to the rest of the cycle, so
        an armed symbol's whole story is in one file. Only keys the EA actually
        wrote are printed -- a missing stamp stays absent rather than being
        rendered as a zero, which would read as "instantaneous".
        """
        phase = d.get("phase", "?")
        reason = d.get("reason", "")
        parts = [f"[LIMIT] {phase}"]

        if phase in ("ARMED", "TRIGGERED", "DONE", "ABORT"):
            parts.append(f"lower={d.get('lower', '')} upper={d.get('upper', '')}")
        if phase == "ARMED":
            parts.append(f"placed {d.get('placed_pb', '?')} buy / "
                         f"{d.get('placed_ps', '?')} sell limits "
                         f"(expected {d.get('expected_buy', '?')} / "
                         f"{d.get('expected_sell', '?')})")
        if phase in ("TRIGGERED", "DONE", "ABORT"):
            side = d.get("trigger_side", "0")
            parts.append(f"trigger_side={side}"
                         + (" (lower)" if side == "1"
                            else " (upper)" if side == "0" else " (none)"))
        if phase in ("DONE", "ABORT"):
            # ms since the trigger, straight off the EA's own clock.
            t_trig = int(d.get("t_trigger_us") or 0)
            for key, label in (("t_cancel_us", "cancel_done"),
                               ("t_burst_us", "burst_done")):
                raw = d.get(key)
                if raw and t_trig:
                    parts.append(f"{label}=+{(int(raw) - t_trig) / 1000.0:.1f}ms")
        if phase == "DONE":
            parts.append(f"burst_ok={d.get('placed_cs', '?')}/"
                         f"{d.get('burst_expected', '?')}")
        if d.get("elapsed_ms"):
            parts.append(f"elapsed={d['elapsed_ms']}ms")
        if d.get("dealscan_fallbacks"):
            # Non-zero means the safety net had to recover a fill the event
            # stream missed: worth knowing, not worth alarming about.
            parts.append(f"dealscan_fallbacks={d['dealscan_fallbacks']}")
        if reason:
            parts.append(f"reason={reason}")

        line = " ".join(parts)
        if phase == "ABORT":
            self.activity_log.log_error(line)
        else:
            self.activity_log.log_info(line)

    async def _limit_open_once(self, moving_leg: str, constant_leg: str):
        """One full limit open attempt.

        Returns ("OK", None) on success, ("REARM", reason) for ARM_TIMEOUT, or
        ("FAIL", reason) for anything else. Never falls back to sequential.
        """
        # 1. EA must be available -- limit_trigger refuses rather than
        #    silently opening in burst mode (doc 08 section 5).
        if (not _bulk_available or self.ea_bridge is None
                or self.ea_status is None
                or not self.ea_status.get("available")):
            return "FAIL", (
                "EA_UNAVAILABLE: "
                f"{self.ea_status.get('reason') if self.ea_status else 'not provisioned'}"
                " — limit_trigger will not fall back to burst/sequential")
        if not await self.ea_bridge.healthy():
            return "FAIL", "EA_UNHEALTHY: not answering a ping"

        info = mt5.symbol_info(self.mt5_symbol)
        tick = mt5.symbol_info_tick(self.mt5_symbol)
        if info is None or tick is None:
            return "FAIL", "NO_TICK_OR_SYMBOL_INFO"

        # 2. Levels from a FRESH mid, with the single floor applied and
        #    logged loudly whenever it moves the offset.
        mid = (tick.ask + tick.bid) / 2
        levels = bulk_orders.compute_limit_levels(
            self.mt5_symbol, mid, self.state.entry_offset, info, tick)
        if levels["clamped"]:
            self.activity_log.log_info(
                f"[LIMIT] entry_offset clamped UP: requested "
                f"{levels['requested_offset']} -> effective "
                f"{levels['effective_offset']} "
                f"(floor {levels['floor']} = max(min_stop_pips "
                f"{levels['min_stop_pips']}, stops_level "
                f"{levels['stops_level']} x point) + spread "
                f"{levels['spread']})")

        # 3. Build both scenarios from their own centers.
        plan = bulk_orders.build_limit_plan(
            self.mt5_symbol, self.state.moving_side,
            self.state.moving_lot, self.state.constant_lot,
            self.state.moving_total, self.state.constant_total,
            moving_tp_level, moving_sl_level,
            self.state.grid_distance, self.state.moving_freq,
            levels["lower"], levels["upper"])
        self.activity_log.log_info(
            f"[LIMIT] plan: lower {plan['lower']} ({plan['pb_count']} buy "
            f"limits), upper {plan['upper']} ({plan['ps_count']} sell "
            f"limits), burst {plan['burst_expected']} order(s), "
            f"{levels['effective_offset']} either side of mid {mid}")

        # 4. Preflight -- loud failure, nothing placed.
        try:
            bulk_orders.preflight_limit(self.mt5_symbol, plan, levels,
                                        self.state.burst_mode)
        except bulk_orders.LimitPreflightError as e:
            self.activity_log.log_error(f"[LIMIT] preflight refused: {e}")
            return "FAIL", f"PREFLIGHT_FAIL: {e}"
        self.activity_log.log_info("[LIMIT] preflight passed")

        # 5. Persist ARMED (phase, both levels, cmd id -- NOT the pending
        #    tickets: recovery sweeps by symbol+magic, so storing them would be
        #    shadow state that can go stale). Doc 08 section 8.
        cmd_id = uuid.uuid4().hex[:8]
        self.state.arm_cmd_id = cmd_id
        self.state.arm_lower = plan["lower"]
        self.state.arm_upper = plan["upper"]
        self.state.phase = "ARMED"
        self._limit_armed = True
        await self._save_symbol_state()

        # 6. Arm and wait for a terminal phase.
        try:
            phase = await self.ea_bridge.arm_limit(
                self.mt5_symbol, self.MAGIC_NUMBER, plan,
                armed_timeout_ms=self.state.armed_timeout_seconds * 1000,
                win_fill_deadline_ms=self.state.win_fill_deadline_ms,
                cancel_ack_deadline_ms=self.state.cancel_ack_deadline_ms,
                burst_mode=self.state.burst_mode,
                cmd_id=cmd_id,
                on_phase=self._log_limit_phase)
        except Exception as e:
            self._limit_armed = False
            return "FAIL", f"ARM_ERROR: {e}"
        finally:
            self._limit_armed = False

        if phase.get("phase") != "DONE":
            reason = phase.get("reason", "UNKNOWN")
            await self._sweep_pendings(f"abort {reason}")
            if reason == "ARM_TIMEOUT":
                return "REARM", "ARM_TIMEOUT"
            return "FAIL", f"EA_ABORT: {reason}"

        # 7. Reconcile against positions_get and orders_get. Python never
        #    trusts the EA's DONE alone (doc 08 section 8). Only the
        #    TRIGGERED scenario's half is expected to be filled -- the losing
        #    ladder was cancelled, so expecting all 8 lines would fail every
        #    single open.
        triggered_roles = ("PB", "CS") if str(phase.get("trigger_side",
                                                        "0")) == "1" \
            else ("PS", "CB")
        tickets, err = await self._reconcile_limit_open(plan, triggered_roles)
        if err:
            await self._sweep_pendings(f"reconcile {err}")
            return "FAIL", f"RECONCILE_FAIL: {err}"

        # 8. Center is the LEVEL THAT TRIGGERED, never the average fill
        #    price, so Python's targets match the anchors the broker used.
        side = str(phase.get("trigger_side", "0"))
        center = plan["lower"] if side == "1" else plan["upper"]
        triggered = "lower" if side == "1" else "upper"
        self.state.center_price = center
        self.state.grid_level_up = center + self.state.grid_distance
        self.state.grid_level_down = center - self.state.grid_distance
        # Explicit center: the level that triggered, NOT the live mid.
        await self._recompute_cycle_levels(center)
        self.state.arm_cmd_id = ""
        self.state.arm_lower = 0.0
        self.state.arm_upper = 0.0
        self.activity_log.log_info(
            f"[LIMIT] center set to the {triggered} level {center} "
            f"(NOT the average fill price); targets recomputed and persisted")
        self._last_open_quality = dict(phase)

        # 9. Shared post-open -- the same routine the burst branch calls.
        await self._register_open_positions(plan["lines"], tickets, moving_leg,
                                           constant_leg,
                                           quality_mode="limit_trigger",
                                           quality_phase=phase,
                                           quality_side=triggered)
        self.activity_log.log_info(
            f"[LIMIT] open complete: {len(tickets)} positions registered")
        return "OK", None

    async def _reconcile_limit_open(self, plan: dict, roles: tuple = None):
        """Verify the open against the broker, never against the EA's word.

        `roles` limits the expectation to the half that should be filled for
        the triggered scenario (PB+CS on a lower trigger, PS+CB on an upper
        one); the losing ladder is expected to be GONE, not filled.

        Returns (tag -> (ticket, entry), error_message). Retries for ~1 s
        because positions_get can lag the deal events the EA has seen.
        """
        lines = [l for l in plan["lines"]
                 if roles is None or l["role"] in roles]
        all_tags = {l["tag"] for l in plan["lines"]}
        by_tag = {o["tag"]: o for o in lines}
        want_buy = [t for t, o in by_tag.items()
                    if o["side"] == "B"]
        want_sell = [t for t, o in by_tag.items()
                     if o["side"] == "S"]

        deadline = time.monotonic() + RECONCILE_RETRY_S
        last_err = "not attempted"
        while True:
            def _snapshot():
                positions = [p for p in (mt5.positions_get(symbol=self.mt5_symbol) or [])
                             if p.magic == self.MAGIC_NUMBER]
                pending = [o.ticket for o in
                           (mt5.orders_get(symbol=self.mt5_symbol) or ())
                           if o.magic == self.MAGIC_NUMBER]
                return positions, pending

            positions, pending = await asyncio.to_thread(_snapshot)

            by_comment = {p.comment: (p.ticket, p.price_open) for p in positions}
            tickets = {t: by_comment[t] for t in by_tag if t in by_comment}

            missing = [t for t in by_tag if t not in tickets]
            got_buy = len([t for t in tickets if by_tag[t]["side"] == "B"])
            got_sell = len([t for t in tickets if by_tag[t]["side"] == "S"])
            want_buy_n = len(want_buy)
            want_sell_n = len(want_sell)

            if not missing and not pending and \
                    got_buy == want_buy_n and got_sell == want_sell_n:
                return tickets, None

            # Positions belonging to the LOSING scenario are just as wrong as
            # missing ones -- both trigger sides filled at once.
            loser = [p.comment for p in positions
                     if p.comment in all_tags and p.comment not in by_tag]
            last_err = (
                f"{len(missing)}/{len(by_tag)} positions missing "
                f"(buy {got_buy}/{want_buy_n}, sell {got_sell}/{want_sell_n}), "
                f"{len(pending)} pending order(s) left"
                + (f", missing tags {missing[:6]}" if missing else "")
                + (f", UNEXPECTED loser-side fills {loser[:6]}" if loser
                   else ""))

            if time.monotonic() >= deadline:
                self.activity_log.log_error(
                    f"[LIMIT] reconcile mismatch after {RECONCILE_RETRY_S:.0f}s "
                    f"retry: {last_err}")
                return {}, last_err
            await asyncio.sleep(RECONCILE_RETRY_DELAY_S)

    async def _ea_close_tickets(self, tickets: List[int]) -> bool:
        """Close 3+ tickets through the EA (plan E.3); False if the EA is
        not usable so the caller keeps the sequential path."""
        if not _bulk_available or self.ea_bridge is None or self.ea_status is None:
            return False
        if not self.ea_status.get("available"):
            return False
        if not await self.ea_bridge.healthy():
            return False
        try:
            res = await self.ea_bridge.close_tickets(
                self.mt5_symbol, self.MAGIC_NUMBER, tickets)
        except Exception as e:
            self.activity_log.log_error(f"EA close_tickets failed: {e}")
            return False
        # Always verify with positions_get afterwards (plan E.3)
        await asyncio.sleep(0.3)
        positions = mt5.positions_get(symbol=self.mt5_symbol) or []
        live = set(p.ticket for p in positions)
        closed = [t for t in tickets if t not in live]
        if len(closed) < len(tickets):
            self.activity_log.log_error(
                f"EA close verified {len(closed)}/{len(tickets)} "
                "— stragglers left for the sequential pass")
            return False
        return True

    async def _force_close_everything(self):
        """Close every open strategy position on this symbol immediately
        (cycle-end remainder, terminate-all, exhausted-queue reset).
        3+ positions go through the EA bulk close when available (plan E.3);
        one or two keep direct order_send. The sequential loop remains the
        verification/fallback pass — the orchestrator's nuclear fallback is
        untouched."""
        positions = mt5.positions_get(symbol=self.mt5_symbol) or []
        mine = [pos.ticket for pos in positions if pos.magic == self.MAGIC_NUMBER]
        ea_closed = 0
        if len(mine) >= 3:
            if await self._ea_close_tickets(mine):
                positions = mt5.positions_get(symbol=self.mt5_symbol) or []
                live = set(p.ticket for p in positions)
                ea_closed = len(mine) - len([t for t in mine if t in live])
                mine = [t for t in mine if t in live]
        closed = ea_closed
        for pos in mt5.positions_get(symbol=self.mt5_symbol) or []:
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
        # An ARMED symbol has no positions to wait for: abort the arm and its
        # pendings instead (doc 08 section 8 lifecycle hooks).
        if self._limit_armed or self.state.phase == "ARMED":
            self.activity_log.log_info(
                f"[LIMIT] stop while ARMED on {self.mt5_symbol}: aborting the "
                "arm and sweeping pendings")
            self._limit_armed = False
            if self.ea_bridge is not None:
                try:
                    await self.ea_bridge.abort_arm(self.mt5_symbol,
                                                   self.MAGIC_NUMBER)
                except Exception as e:
                    self.activity_log.log_error(
                        f"[LIMIT] abort_arm during stop raised: {e}")
            await self._sweep_pendings("graceful stop while armed")
            self.state.phase = "IDLE"
            self.state.arm_cmd_id = ""
            self.state.arm_lower = 0.0
            self.state.arm_upper = 0.0
            self.running = False
            self.graceful_stop = True
            await self._save_symbol_state()
            self.activity_log.log_stop(self.state.cycle_count,
                                       "graceful_stop_armed_aborted")
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
        # Sweep BEFORE closing: while armed there may be pendings that would
        # otherwise fill during the close and leave a new position behind.
        self._limit_armed = False
        await self._sweep_pendings("terminate before close")
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
        # Sweep AFTER the close too, so a pending that filled mid-close cannot
        # survive the terminate (doc 08 section 8 lifecycle hooks).
        await self._sweep_pendings("terminate after close")
        self.state.arm_cmd_id = ""
        self.state.arm_lower = 0.0
        self.state.arm_upper = 0.0
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
            "open_mode": self.state.open_mode,
            "armed": self._limit_armed or self.state.phase == "ARMED",
            "consecutive_open_failures": self._consecutive_open_failures,
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
            await self._process_close_queue(ask, bid)

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

    async def _process_close_queue(self, ask: float, bid: float):
        """Attempt to close each pending queue item whose target price has
        actually been reached by the market — never close on release alone.
        A "down" target fires once bid has fallen to/through it; an "up"
        target fires once ask has risen to/through it. Items not yet reached
        stay queued untouched and are re-checked next tick. On a close
        failure (target reached but broker rejects), requeue with
        retry_count incremented; if the failing item is the last pending
        one, terminate all and restart the cycle (agent/02 §8).
        Iterate a snapshot — the real list mutates during the pass."""
        for item in list(self.state.close_queue):
            target = item.target
            price_reached = (
                bid <= target.price if target.direction == "down"
                else ask >= target.price
            )
            if not price_reached:
                continue  # not yet at this target's level — leave queued

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
            "open_mode": self.state.open_mode,
            "entry_offset": self.state.entry_offset,
            "burst_mode": self.state.burst_mode,
            "win_fill_deadline_ms": self.state.win_fill_deadline_ms,
            "cancel_ack_deadline_ms": self.state.cancel_ack_deadline_ms,
            "armed_timeout_seconds": self.state.armed_timeout_seconds,
            # ARMED recovery context: phase, both levels and the command id.
            # The pending ticket set is deliberately NOT persisted -- recovery
            # sweeps by symbol+magic, so stored tickets would be shadow state.
            "arm_lower": self.state.arm_lower,
            "arm_upper": self.state.arm_upper,
            "arm_cmd_id": self.state.arm_cmd_id,
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
        the entire pass (agent/05: no live tick may interleave mid-replay).

        On success the engine is left RUNNING so the recovered positions are
        actually managed again — rebuilding state without going live would
        leave every open position unmanaged and silently bleeding.
        """
        await self._ensure_repository_async()

        summary = {"replayed": [], "orphans": [], "stale_constant_tickets": [],
                   "cycle_restarted": False, "resumed": False,
                   "pendings_swept": [], "armed_recovered": False}

        # Any pending carrying our magic is removed FIRST, before anything
        # else can interact with it. Scoped to symbol+magic because the magic
        # is shared across symbols (doc 08 section 8).
        try:
            swept = await self._sweep_pendings("startup reconcile")
            summary["pendings_swept"].append(
                {"symbol": self.mt5_symbol, "swept": bool(swept)})
        except Exception as e:
            self.activity_log.log_error(
                f"[LIMIT] startup pending sweep raised on "
                f"{self.mt5_symbol}: {e}")

        state_row = await self.repository.get_state()
        persisted_phase = (state_row or {}).get("phase")

        # Seed last-known prices from a live tick BEFORE replay: closure
        # direction is inferred from them when deal history is unavailable, and
        # at startup they would still be 0.0 (which picks the wrong side).
        tick = mt5.symbol_info_tick(self.mt5_symbol)
        if tick:
            self._last_ask = tick.ask
            self._last_bid = tick.bid

        async with self.execution_lock:
            self.state.catching_up = True
            try:
                state_row = await self.repository.get_state()
                if persisted_phase == "ARMED":
                    # Armed with pendings after a restart means cancel and go
                    # idle — never resume an armed state (doc 08 section 3.6).
                    # The sweep above already removed the pendings; flatten
                    # anything the ladder filled and restart idle.
                    summary["armed_recovered"] = True
                    self.activity_log.log_info(
                        f"[RECOVERY] persisted phase was ARMED on "
                        f"{self.mt5_symbol}: pendings swept, flattening any "
                        f"positions the ladder opened and restarting idle")
                    await self._force_close_everything()
                    repo = self.repository
                    if repo is not None:
                        await repo.clear_constant_queue()
                        await repo.clear_constant_tickets()
                        await repo.clear_moving_positions()
                    self._clear_cycle_state()
                    self.state.phase = "IDLE"
                    self.running = False
                    self.recovered = False
                    return summary
                if state_row and state_row.get("phase") in ("ACTIVE", "RESETTING"):
                    await self._rebuild_state_from_db(state_row)
                    await self._verify_constant_tickets(summary)
                    await self._replay_missed_closures(summary)
                else:
                    # No active cycle persisted — clean slate
                    self.state.phase = "IDLE"
                    self.state.catching_up = False
                    return summary
            finally:
                self.state.catching_up = False

        # A RESETTING row means the process died mid-cycle-end. Positions may
        # still be open; resume into ACTIVE so the normal tick path drains them
        # rather than leaving a half-dead cycle.
        if self.state.phase == "RESETTING":
            self.state.phase = "ACTIVE"

        if (self.state.moving_positions or self.state.constant_tickets
                or self.state.close_queue):
            self.recovered = True
            self.running = True
            self.graceful_stop = False
            summary["resumed"] = True

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
        self.state.open_mode = metadata.get("open_mode", "burst")
        self.state.entry_offset = float(metadata.get("entry_offset", 50.0) or 50.0)
        self.state.burst_mode = metadata.get("burst_mode", "AFTER_CANCEL")
        self.state.win_fill_deadline_ms = int(
            metadata.get("win_fill_deadline_ms", 1500) or 1500)
        self.state.cancel_ack_deadline_ms = int(
            metadata.get("cancel_ack_deadline_ms", 1500) or 1500)
        self.state.armed_timeout_seconds = int(
            metadata.get("armed_timeout_seconds", 120) or 120)
        self.state.arm_lower = float(metadata.get("arm_lower", 0.0) or 0.0)
        self.state.arm_upper = float(metadata.get("arm_upper", 0.0) or 0.0)
        self.state.arm_cmd_id = metadata.get("arm_cmd_id", "") or ""
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

    async def _verify_constant_tickets(self, summary: dict):
        """Drop stored constant tickets that are no longer open at the broker.

        A constant position can be closed by the user (or by the broker) while
        the bot is down. The DB row survives, so the engine would later try to
        close a ticket that no longer exists, fail, and — if it is the last
        pending queue item — hit the terminate-all-and-restart-cycle path for
        no reason. Positions_get is the source of truth: keep only the tickets
        that are genuinely still open.
        """
        stored = list(self.state.constant_tickets)
        if not stored:
            return

        positions = mt5.positions_get(symbol=self.mt5_symbol) or []
        live = {p.ticket for p in positions if p.magic == self.MAGIC_NUMBER}

        stale = [t for t in stored if t not in live]
        if not stale:
            return

        self.state.constant_tickets = [t for t in stored if t in live]
        for ticket in stale:
            await self.repository.delete_constant_ticket(ticket)
        summary["stale_constant_tickets"] = stale
        self.activity_log.log_info(
            f"[RECOVERY] Dropped {len(stale)} constant ticket(s) no longer open "
            f"at the broker: {stale}")

    async def _replay_missed_closures(self, summary: dict):
        """Find DB-tracked open tickets missing from MT5, pull their deal
        history, and replay each closure through _handle_moving_closure — the
        exact function a live tick uses."""
        positions = mt5.positions_get(symbol=self.mt5_symbol)
        live_tickets = set(p.ticket for p in positions) if positions else set()

        tracked_open = set(self.state.moving_positions.keys())
        missing = tracked_open - live_tickets

        # Live positions with our magic that the DB never recorded (crash
        # between the broker fill and the DB write). Report them rather than
        # adopting them: their slot index is unknowable, so guessing one would
        # release the wrong constant target later.
        orphans = sorted(t for t in live_tickets - tracked_open
                         if t not in set(self.state.constant_tickets))
        if orphans:
            summary["orphans"] = orphans
            self.activity_log.log_info(
                f"[RECOVERY] {len(orphans)} live position(s) not tracked by this "
                f"strategy — left unmanaged, close manually if unwanted: {orphans}")

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
