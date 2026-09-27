from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Tuple, List, Any
import asyncio
import json
import time
import logging
import MetaTrader5 as mt5_module
from datetime import datetime

from core.engine.activity_logger import ActivityLogger
from core.persistence.repository import Repository

logger = logging.getLogger("pair_strategy")
mt5: Any = mt5_module

# --- Hardcoded immutable asset limits (module-level constants) ---
MAX_LOT_PER_ASSET = {
    "FX Vol 20": 7,
    "FX Vol 40": 4,
    "FX Vol 60": 5,
    "FX Vol 80": 1,
    "FX Vol 99": 4,
    "SFX Vol 20": 5,
    "SFX Vol 40": 1,
    "SFX Vol 60": 1,
    "SFX Vol 80": 2,
    "SFX Vol 99": 2,
}

MIN_STOP_PIPS_PER_ASSET = {
    "FX Vol 20": 11,
    "FX Vol 40": 27,
    "FX Vol 60": 19,
    "FX Vol 80": 34,
    "FX Vol 99": 42,
    "SFX Vol 20": 21,
    "SFX Vol 40": 74,
    "SFX Vol 60": 59,
    "SFX Vol 80": 86,
    "SFX Vol 99": 18,
}



@dataclass
class GridLevel:
    """Represents a single grid level with its positions"""
    price: float
    active: bool = False
    reference_buy_tp: Optional[float] = None
    reference_buy_sl: Optional[float] = None
    reference_sell_tp: Optional[float] = None
    reference_sell_sl: Optional[float] = None
    reference_custom_buy_tp: Optional[float] = None
    reference_custom_buy_sl: Optional[float] = None
    reference_custom_sell_tp: Optional[float] = None
    reference_custom_sell_sl: Optional[float] = None
    
    # Position tracking (ticket -> {leg, direction, entry, tp, sl, lot})
    positions: Dict[int, dict] = field(default_factory=dict)

    
    def get_buy_tickets(self) -> List[int]:
        """Get all BUY tickets at this level (for FIFO closing)"""
        return [t for t, info in self.positions.items() if info['direction'] == 'buy']
    
    def get_sell_tickets(self) -> List[int]:
        """Get all SELL tickets at this level (for FIFO closing)"""
        return [t for t, info in self.positions.items() if info['direction'] == 'sell']


@dataclass
class SetState:
    set_index: int
    phase: str = "IDLE"
    grid_level_1: Optional[GridLevel] = None
    grid_level_2: Optional[GridLevel] = None
    position_counter: int = 0
    last_move_direction: str = ""
    is_final_group_reached: bool = False


@dataclass
class StrategyState:
    """Complete state for Grid Bounce Strategy"""
    phase: str = "IDLE"  # IDLE, SINGLE_LEVEL, TWO_LEVELS, RESETTING
    
    # Grid configuration
    center_price: float = 0.0  # Initial startup price
    
    total_positions: int = 0   # Total open positions (for tracking)
    
    # Cycle tracking
    cycle_count: int = 0
    realized_pnl: float = 0.0

    # Per-set state
    sets: List[SetState] = field(default_factory=list)
    
    # Ticket tracking (global across all levels)
    ticket_map: Dict[int, dict] = field(default_factory=dict)
    ticket_touch_flags: Dict[int, dict] = field(default_factory=dict)
    split_group_map: Dict[int, List[int]] = field(default_factory=dict)


# core logic for managing the 2-grid bounce strategy
class GridBounceStrategyEngine:
    """
    2-Grid Level Bouncing Strategy Engine
    
    Lifecycle:
    1. Start at center → open initial BUY + SELL pair
    2. Wait for grid_distance move (up or down)
    3. On move: close opposite position at origin, open 3 new at destination
    4. Bounce between 2 levels until TP/SL nuclear reset
    5. Reset → restart from current price as new center
    """
    
    MAGIC_NUMBER = 123456
    
    def __init__(self, config_manager, symbol: str, user_id: str = "default", 
                 session_logger=None):
        self.config_manager = config_manager
        self.symbol = symbol
        self.user_id = user_id
        self.session_logger = session_logger
        
        self.state = StrategyState()
        self.running = False
        self.graceful_stop = False
        
        self.execution_lock = asyncio.Lock()
        self.activity_log = ActivityLogger(symbol, user_id, session_logger)
        self.repository: Optional[Repository] = None
        self._position_drop_detected_set_indices: set[int] = set()
        self._last_known_spread = 0.0
        self.orphan_tickets: List[int] = []
    
    # Config accessors
    @property
    def config(self) -> Dict[str, Any]:
        return self.config_manager.get_symbol_config(self.symbol) or {}

    @property
    def grid_distance(self) -> float:
        return float(self.config.get('grid_distance', 50.0))
    
    @property
    def num_sets(self) -> int:
        """Get total number of sets configured"""
        return int(self.config.get('sets', 1))
    
    @property
    def current_set_config(self) -> Dict[str, Any]:
        """Get configuration for current active set"""
        return self._get_set_config(0)

    def _get_set_config(self, set_index: int) -> Dict[str, Any]:
        sets_config = self.config.get('sets_config', [])
        if sets_config:
            idx = max(0, min(set_index, len(sets_config) - 1))
            return sets_config[idx]

        return {
            'center_buy_lot': self.config.get('center_buy_lot', self.config.get('pair_buy_lot', 0.01)),
            'center_sell_lot': self.config.get('center_sell_lot', self.config.get('pair_sell_lot', 0.01)),
            'pair_buy_lots': self.config.get('pair_buy_lots', [0.01]),
            'pair_sell_lots': self.config.get('pair_sell_lots', [0.01]),
            'single_lots': self.config.get('single_lots', [0.01]),
            'max_positions': self.config.get('max_positions', 3),
        }

    def _ensure_set_state(self, set_index: int) -> SetState:
        while len(self.state.sets) <= set_index:
            self.state.sets.append(SetState(set_index=len(self.state.sets)))
        set_state = self.state.sets[set_index]
        if set_state.set_index != set_index:
            set_state.set_index = set_index
        return set_state

    def _group_count_for_set(self, set_index: int) -> int:
        return max(1, int(self._get_set_config(set_index).get('max_positions', 3)) // 3)

    def _center_buy_lot_for_set(self, set_index: int) -> float:
        cfg = self._get_set_config(set_index)
        value = cfg.get('center_buy_lot', cfg.get('pair_buy_lot', 0.01))
        return max(0.01, float(value))

    def _center_sell_lot_for_set(self, set_index: int) -> float:
        cfg = self._get_set_config(set_index)
        value = cfg.get('center_sell_lot', cfg.get('pair_sell_lot', 0.01))
        return max(0.01, float(value))

    def _pair_buy_lots_for_set(self, set_index: int) -> List[float]:
        cfg = self._get_set_config(set_index)
        lots = cfg.get('pair_buy_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(cfg.get('pair_buy_lot', 0.01)))]
        need = self._group_count_for_set(set_index)
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    def _pair_sell_lots_for_set(self, set_index: int) -> List[float]:
        cfg = self._get_set_config(set_index)
        lots = cfg.get('pair_sell_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(cfg.get('pair_sell_lot', 0.01)))]
        need = self._group_count_for_set(set_index)
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    def _single_lots_for_set(self, set_index: int) -> List[float]:
        cfg = self._get_set_config(set_index)
        lots = cfg.get('single_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(cfg.get('single_lot', 0.01)))]
        need = self._group_count_for_set(set_index)
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    def _pair_buy_lot_for_set_stage(self, set_index: int, stage_idx: int) -> float:
        lots = self._pair_buy_lots_for_set(set_index)
        idx = max(0, min(stage_idx, len(lots) - 1))
        return lots[idx]

    def _pair_sell_lot_for_set_stage(self, set_index: int, stage_idx: int) -> float:
        lots = self._pair_sell_lots_for_set(set_index)
        idx = max(0, min(stage_idx, len(lots) - 1))
        return lots[idx]

    def _single_lot_for_set_group(self, set_index: int, group_idx: int) -> float:
        lots = self._single_lots_for_set(set_index)
        idx = max(0, min(group_idx, len(lots) - 1))
        return lots[idx]

    def _set_display(self, set_index: int) -> str:
        return f"Set {set_index + 1}/{self.num_sets}" if self.num_sets > 1 else f"Set {set_index + 1}"

    def _get_tickets_for_set(self, set_index: int) -> List[int]:
        """Returns all tickets currently tracked under the given set_index."""
        return [
            t for t, info in self.state.ticket_map.items()
            if info and info.get('set_index', 0) == set_index
        ]
    
    @property
    def max_positions(self) -> int:
        """Max positions for current set"""
        return int(self.current_set_config.get('max_positions', 3))

    @property
    def group_count(self) -> int:
        return max(1, self.max_positions // 3)

    @property
    def pair_buy_lots(self) -> List[float]:
        lots = self.current_set_config.get('pair_buy_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(self.current_set_config.get('pair_buy_lot', 0.01)))]
        need = self.group_count + 1  # center + each 3-position group
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    @property
    def pair_sell_lots(self) -> List[float]:
        lots = self.current_set_config.get('pair_sell_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(self.current_set_config.get('pair_sell_lot', 0.01)))]
        need = self.group_count + 1
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    @property
    def single_lots(self) -> List[float]:
        lots = self.current_set_config.get('single_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(self.current_set_config.get('single_lot', 0.01)))]
        need = self.group_count
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    def _pair_buy_lot_for_stage(self, stage_idx: int) -> float:
        lots = self.pair_buy_lots
        idx = max(0, min(stage_idx, len(lots) - 1))
        return lots[idx]

    def _pair_sell_lot_for_stage(self, stage_idx: int) -> float:
        lots = self.pair_sell_lots
        idx = max(0, min(stage_idx, len(lots) - 1))
        return lots[idx]

    def _single_lot_for_group(self, group_idx: int) -> float:
        lots = self.single_lots
        idx = max(0, min(group_idx, len(lots) - 1))
        return lots[idx]
    
    @property
    def pair_buy_lot(self) -> float:
        return float(self.config.get('pair_buy_lot', 0.01))
    
    @property
    def pair_sell_lot(self) -> float:
        return float(self.config.get('pair_sell_lot', 0.01))
    
    @property
    def single_lot(self) -> float:
        return float(self.config.get('single_lot', 0.01))
    
    @property
    def tp_pips(self) -> float:
        return float(self.config.get('tp_pips', 150.0))
    
    @property
    def sl_pips(self) -> float:
        return float(self.config.get('sl_pips', 200.0))
    
    @property
    def second_entry_buy_tp_pips(self) -> float:
        """TP pips for unpaired BUY single trades (2nd entry system)"""
        return float(self.config.get('second_entry_buy_tp_pips', self.tp_pips))
    
    @property
    def second_entry_buy_sl_pips(self) -> float:
        """SL pips for unpaired BUY single trades (2nd entry system)"""
        return float(self.config.get('second_entry_buy_sl_pips', self.sl_pips))
    
    @property
    def second_entry_sell_tp_pips(self) -> float:
        """TP pips for unpaired SELL single trades (2nd entry system)"""
        return float(self.config.get('second_entry_sell_tp_pips', self.tp_pips))
    
    @property
    def second_entry_sell_sl_pips(self) -> float:
        """SL pips for unpaired SELL single trades (2nd entry system)"""
        return float(self.config.get('second_entry_sell_sl_pips', self.sl_pips))

    @property
    def current_price(self) -> float:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick:
            return (tick.ask + tick.bid) / 2
        return self.state.center_price

    @property
    def volatility_tolerance_factor(self):
        val = self.config_manager.get_global_config().get('volatility_tolerance', 'off')
        mapping = {
            "1.5": 1.5,
            "1.75": 1.75,
            "2.0": 2.0,
            "2.25": 2.25,
            "2.5": 2.5
        }
        return mapping.get(val, None)

    def get_set_display(self, set_index: int) -> str:
        """Get a display string showing a specific set's info."""
        if self.num_sets > 1:
            return f"Set {set_index + 1}/{self.num_sets}"
        return f"Set {set_index + 1}"

    async def start_ticker(self):
        """Compatibility hook for orchestrator config refreshes."""
        return None
    

#startup logic and main loop

    async def start(self):
        """
        Start strategy - open initial BUY + SELL at center price
        """
        if self.running:
            return
        
        self._reset_state()
        self.running = True
        self.graceful_stop = False
        self._position_drop_detected_set_indices.clear()
        
        # Get current tick
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.activity_log.log_error("Failed to get tick for start")
            return
        
        center = (tick.ask + tick.bid) / 2
        self.state.center_price = center
        await self._open_center_pair_for_set(0, center, log_start=True)
        await self.save_state()

    async def _open_center_pair_for_set(self, set_index: int, center: float, log_start: bool = False):
        """Open the center buy/sell pair for a specific set."""
        set_state = self._ensure_set_state(set_index)
        set_state.phase = "SINGLE_LEVEL"
        set_state.grid_level_1 = GridLevel(price=center, active=True)
        set_state.grid_level_2 = None
        set_state.position_counter = 0
        set_state.last_move_direction = ""
        set_state.is_final_group_reached = False

        if log_start:
            self.activity_log.log_start(self.state.cycle_count, center, set_index=set_index)
        if self.num_sets > 1:
            self.activity_log.log_info(
                f"Starting strategy on {self._set_display(set_index)} | "
                f"max_positions={self._get_set_config(set_index).get('max_positions', 3)}, "
                f"pair_buy={self._pair_buy_lots_for_set(set_index)}, "
                f"pair_sell={self._pair_sell_lots_for_set(set_index)}, "
                f"single={self._single_lots_for_set(set_index)}",
                set_index=set_index,
            )

        center_buy_lot = self._center_buy_lot_for_set(set_index)
        center_sell_lot = self._center_sell_lot_for_set(set_index)
        buy_results = await self._split_and_execute_orders("buy", center_buy_lot, "CenterBuy", center, skip_tp_sl=True)
        sell_results = await self._split_and_execute_orders("sell", center_sell_lot, "CenterSell", center, skip_tp_sl=True)

        self.activity_log.log_info(
            "Center positions opened without TP/SL (will be added after second entry)",
            set_index=set_index,
        )

        center_level = set_state.grid_level_1
        total_opened = 0
        if center_level:
            for (tkt, entry, tp, sl) in buy_results:
                if not tkt:
                    continue
                await self._record_set_position(set_index, center_level, tkt, 'CenterBuy', 'buy', entry, 0.0, 0.0, center_buy_lot, 'pair')
                self.activity_log.log_fire(
                    self.state.cycle_count, "CenterBuy", entry,
                    center_buy_lot, tp,
                    sl, tkt, set_index=set_index
                )
                total_opened += 1

            for (tkt, entry, tp, sl) in sell_results:
                if not tkt:
                    continue
                await self._record_set_position(set_index, center_level, tkt, 'CenterSell', 'sell', entry, 0.0, 0.0, center_sell_lot, 'pair')
                self.activity_log.log_fire(
                    self.state.cycle_count, "CenterSell", entry,
                    center_sell_lot, tp,
                    sl, tkt, set_index=set_index
                )
                total_opened += 1

        self.state.total_positions += total_opened

    async def _process_all_set_triggers(self, ask: float, bid: float):
        for set_index, set_state in enumerate(list(self.state.sets)):
            if set_state.phase in {"IDLE", "CAPPED", "RESETTING"}:
                continue
            await self._process_set_grid_triggers(set_index, ask, bid)

    async def _process_set_grid_triggers(self, set_index: int, ask: float, bid: float):
        set_state = self._ensure_set_state(set_index)
        if set_state.phase in {"IDLE", "CAPPED", "RESETTING"}:
            return

        mid = (ask + bid) / 2
        grid_dist = self.grid_distance

        if set_state.phase == "SINGLE_LEVEL":
            if not set_state.grid_level_1:
                return
            center = set_state.grid_level_1.price
            if mid <= center - grid_dist:
                await self._activate_second_level_for_set(set_index, "DOWN", ask, bid)
                return
            if mid >= center + grid_dist:
                await self._activate_second_level_for_set(set_index, "UP", ask, bid)
                return

        if set_state.phase == "TWO_LEVELS":
            if not set_state.grid_level_1 or not set_state.grid_level_2:
                return

            level_1_price = set_state.grid_level_1.price
            level_2_price = set_state.grid_level_2.price
            upper_price = max(level_1_price, level_2_price)
            lower_price = min(level_1_price, level_2_price)

            upper_level = set_state.grid_level_1 if level_1_price == upper_price else set_state.grid_level_2
            lower_level = set_state.grid_level_1 if level_1_price == lower_price else set_state.grid_level_2

            if mid <= lower_price and set_state.last_move_direction != "DOWN_TO_LOWER":
                await self._bounce_set(set_index, upper_level, lower_level, ask, bid, "DOWN")
                return

            if mid >= upper_price and set_state.last_move_direction != "UP_TO_UPPER":
                await self._bounce_set(set_index, lower_level, upper_level, ask, bid, "UP")
                return

    def _sync_legacy_state_from_set(self, set_index: int):
        return

    async def _record_set_position(
        self,
        set_index: int,
        grid_level: GridLevel,
        ticket: int,
        leg_name: str,
        direction: str,
        entry: float,
        tp: float,
        sl: float,
        lot: float,
        position_type: str,
    ):
        position = {
            'leg': leg_name,
            'direction': direction,
            'entry': entry,
            'tp': tp,
            'sl': sl,
            'lot': lot,
            'position_type': position_type,
            'set_index': set_index,
        }
        grid_level.positions[ticket] = position
        self.state.ticket_map[ticket] = position
        self._init_touch_flags(ticket)

        if self.repository is not None:
            grid_level_index = 1
            set_state = self._ensure_set_state(set_index)
            if set_state.grid_level_2 is grid_level:
                grid_level_index = 2
            await self.repository.save_ticket(
                ticket=ticket,
                cycle_id=self.state.cycle_count,
                pair_index=0,
                leg=leg_name,
                trade_count=0,
                entry_price=entry,
                tp_price=tp,
                sl_price=sl,
                set_index=set_index,
                grid_level=grid_level_index,
            )

    async def _open_set_orders(
        self,
        set_index: int,
        grid_level: GridLevel,
        ask: float,
        bid: float,
        direction: str,
        lot: float,
        leg_name: str,
        position_type: str,
        tp_pips_override: Optional[float] = None,
        sl_pips_override: Optional[float] = None,
        skip_tp_sl: bool = False,
    ) -> List[Tuple[int, float, float, float]]:
        results = await self._split_and_execute_orders(
            direction,
            lot,
            leg_name,
            grid_level.price,
            tp_pips_override=tp_pips_override,
            sl_pips_override=sl_pips_override,
            skip_tp_sl=skip_tp_sl,
        )

        tickets = []
        for (tkt, entry, tp, sl) in results:
            if not tkt:
                continue
            await self._record_set_position(set_index, grid_level, tkt, leg_name, direction, entry, tp, sl, lot, position_type)
            self.activity_log.log_fire(
                self.state.cycle_count,
                leg_name,
                entry,
                lot,
                tp,
                sl,
                tkt,
                set_index=set_index,
            )
            tickets.append(tkt)

        if len(tickets) > 1:
            group_id = tickets[0]
            self.state.split_group_map[group_id] = list(tickets)
            for ticket in tickets:
                if ticket in self.state.ticket_map:
                    self.state.ticket_map[ticket]['split_group_id'] = group_id

        return results

    async def _open_group_for_set(
        self,
        set_index: int,
        grid_level: GridLevel,
        ask: float,
        bid: float,
        direction: str,
        current_group: int,
    ) -> bool:
        set_state = self._ensure_set_state(set_index)
        group_count = self._group_count_for_set(set_index)

        if current_group > group_count:
            set_state.phase = "CAPPED"
            self._sync_legacy_state_from_set(set_index)
            return False

        pair_stage_idx = max(0, current_group - 1)
        pair_buy_lot = self._pair_buy_lot_for_set_stage(set_index, pair_stage_idx)
        pair_sell_lot = self._pair_sell_lot_for_set_stage(set_index, pair_stage_idx)
        single_lot = self._single_lot_for_set_group(set_index, pair_stage_idx)

        if current_group < group_count:
            if direction == "UP":
                await self._open_set_orders(set_index, grid_level, ask, bid, "buy", pair_buy_lot, "Buy1", "pair", skip_tp_sl=True)
                await self._open_set_orders(set_index, grid_level, ask, bid, "sell", single_lot, "SingleSell", "single_custom", self.second_entry_sell_tp_pips, self.second_entry_sell_sl_pips)
                await self._open_set_orders(set_index, grid_level, ask, bid, "buy", pair_sell_lot, "Buy2", "pair", skip_tp_sl=True)
            else:
                await self._open_set_orders(set_index, grid_level, ask, bid, "buy", single_lot, "SingleBuy", "single_custom", self.second_entry_buy_tp_pips, self.second_entry_buy_sl_pips)
                await self._open_set_orders(set_index, grid_level, ask, bid, "sell", pair_buy_lot, "Sell1", "pair", skip_tp_sl=True)
                await self._open_set_orders(set_index, grid_level, ask, bid, "sell", pair_sell_lot, "Sell2", "pair", skip_tp_sl=True)
            set_state.position_counter += 3
            self._sync_legacy_state_from_set(set_index)
            return True

        if current_group == group_count:
            if direction == "UP":
                await self._open_set_orders(set_index, grid_level, ask, bid, "sell", single_lot, "SingleSell", "single_custom", self.second_entry_sell_tp_pips, self.second_entry_sell_sl_pips)
            else:
                await self._open_set_orders(set_index, grid_level, ask, bid, "buy", single_lot, "SingleBuy", "single_custom", self.second_entry_buy_tp_pips, self.second_entry_buy_sl_pips)
            set_state.position_counter += 1
            set_state.is_final_group_reached = True
            set_state.phase = "CAPPED"
            self._sync_legacy_state_from_set(set_index)
            await self._handoff_to_next_set(set_index, grid_level.price, ask, bid)
            return True

        set_state.phase = "CAPPED"
        self._sync_legacy_state_from_set(set_index)
        return False

    async def _handoff_to_next_set(self, set_index: int, price: float, ask: float, bid: float):
        next_index = set_index + 1
        current_state = self._ensure_set_state(set_index)
        if next_index >= self.num_sets:
            current_state.phase = "CAPPED"
            self._sync_legacy_state_from_set(set_index)
            return False

        current_state.phase = "CAPPED"
        await self._open_center_pair_for_set(next_index, price, log_start=True)
        next_state = self._ensure_set_state(next_index)
        next_state.phase = "SINGLE_LEVEL"
        next_state.grid_level_1 = GridLevel(price=price, active=True)
        next_state.grid_level_2 = None
        next_state.position_counter = 0
        next_state.last_move_direction = ""
        next_state.is_final_group_reached = False
        if next_index == 0:
            self._sync_legacy_state_from_set(next_index)
        await self.save_state()
        return True

    async def _activate_second_level_for_set(self, set_index: int, direction: str, ask: float, bid: float):
        set_state = self._ensure_set_state(set_index)
        center_level = set_state.grid_level_1
        if not center_level:
            return

        new_price = center_level.price + self.grid_distance if direction == "UP" else center_level.price - self.grid_distance
        self.activity_log.log_info(f"Moving {direction}: Grid distance reached at {new_price:.2f}", set_index=set_index)

        if direction == "DOWN":
            sell_tickets = center_level.get_sell_tickets()
            if sell_tickets:
                oldest_sell = sell_tickets[0]
                if self._close_position(oldest_sell):
                    self.activity_log.log_info(f"Closed SELL at center (ticket {oldest_sell})", set_index=set_index)
                    self._remove_ticket_from_tracking(oldest_sell, center_level)
        else:
            buy_tickets = center_level.get_buy_tickets()
            if buy_tickets:
                oldest_buy = buy_tickets[0]
                if self._close_position(oldest_buy):
                    self.activity_log.log_info(f"Closed BUY at center (ticket {oldest_buy})", set_index=set_index)
                    self._remove_ticket_from_tracking(oldest_buy, center_level)

        set_state.grid_level_2 = GridLevel(price=new_price, active=True)
        set_state.phase = "TWO_LEVELS"
        set_state.last_move_direction = "DOWN_TO_LOWER" if direction == "DOWN" else "UP_TO_UPPER"
        self.activity_log.log_grid_activation("Lower Level" if direction == "DOWN" else "Upper Level", new_price, set_index=set_index)

        current_group = set_state.position_counter // 3 + 1
        await self._open_group_for_set(set_index, set_state.grid_level_2, ask, bid, direction, current_group)
        self._sync_legacy_state_from_set(set_index)
        await self._apply_anchor_alignment_for_set(set_index)
        await self.save_state()

    def _compute_anchors_for_set(self, set_index: int) -> tuple[float, float]:
        set_state = self._ensure_set_state(set_index)
        level_1 = set_state.grid_level_1.price if set_state.grid_level_1 else self.state.center_price
        level_2 = set_state.grid_level_2.price if set_state.grid_level_2 else self.state.center_price
        upper = max(level_1, level_2)
        lower = min(level_1, level_2)
        sl_dist = float(self.sl_pips)
        return upper + sl_dist, lower - sl_dist

    async def _apply_anchor_alignment_for_set(self, set_index: int):
        """Apply anchor TP/SL to all pair positions that belong to one set."""
        set_state = self._ensure_set_state(set_index)
        upper_anchor, lower_anchor = self._compute_anchors_for_set(set_index)

        self.activity_log.log_info(
            f"Applying anchor alignment: upper={upper_anchor:.5f}, lower={lower_anchor:.5f} (sl_pips={self.sl_pips})",
            set_index=set_index,
        )

        for ticket, info in list(self.state.ticket_map.items()):
            if not info or info.get('set_index', 0) != set_index:
                continue
            if info.get('position_type', 'pair') != 'pair':
                continue

            direction = info.get('direction', '')
            new_tp = upper_anchor if direction == 'buy' else lower_anchor
            new_sl = lower_anchor if direction == 'buy' else upper_anchor

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": self.symbol,
                "position": ticket,
                "tp": float(new_tp),
                "sl": float(new_sl),
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                info['tp'] = new_tp
                info['sl'] = new_sl
                for level in [set_state.grid_level_1, set_state.grid_level_2]:
                    if level and ticket in level.positions:
                        level.positions[ticket]['tp'] = new_tp
                        level.positions[ticket]['sl'] = new_sl

    async def _bounce_set(self, set_index: int, source_level: GridLevel, destination_level: GridLevel,
                          ask: float, bid: float, direction: str):
        set_state = self._ensure_set_state(set_index)
        if direction == "DOWN":
            self.activity_log.log_info(f"Bouncing DOWN to {destination_level.price:.2f}", set_index=set_index)
            sell_tickets = source_level.get_sell_tickets()
            if sell_tickets:
                oldest_sell = sell_tickets[0]
                if self._close_position(oldest_sell):
                    self.activity_log.log_info(f"Closed SELL at upper (ticket {oldest_sell})", set_index=set_index)
                    self._remove_ticket_from_tracking(oldest_sell, source_level)
        else:
            self.activity_log.log_info(f"Bouncing UP to {destination_level.price:.2f}", set_index=set_index)
            buy_tickets = source_level.get_buy_tickets()
            if buy_tickets:
                oldest_buy = buy_tickets[0]
                if self._close_position(oldest_buy):
                    self.activity_log.log_info(f"Closed BUY at lower (ticket {oldest_buy})", set_index=set_index)
                    self._remove_ticket_from_tracking(oldest_buy, source_level)

        current_group = set_state.position_counter // 3 + 1
        await self._open_group_for_set(set_index, destination_level, ask, bid, direction, current_group)
        set_state.last_move_direction = "DOWN_TO_LOWER" if direction == "DOWN" else "UP_TO_UPPER"
        self._sync_legacy_state_from_set(set_index)
        await self._apply_anchor_alignment_for_set(set_index)
        await self.save_state()

    #tick handler - same as old one

    async def on_external_tick(self, tick_data: dict):
        """
        Called by orchestrator on every tick
        """
        ask = tick_data.get('ask', 0.0)
        bid = tick_data.get('bid', 0.0)
        raw_spread = ask - bid
        if raw_spread > 0:
            self._last_known_spread = raw_spread

        if not self.running or not self.state.sets or not any(s.phase not in {"IDLE"} for s in self.state.sets):
            return
        
        if ask <= 0 or bid <= 0:
            return
        
        async with self.execution_lock:
            # 1. Check virtual TP/SL first so manual closures behave like real ones
            await self._check_virtual_stops(ask, bid)

            # 1. Volatility/slippage tolerant reset check (new)
            await self._check_volatility_slippage(ask, bid)

            # 2. Update touch flags FIRST (PRESERVED)
            self._update_touch_flags(ask, bid)
            
            # 3. Check position drops (TP/SL detection) (PRESERVED)
            await self._check_position_drops(ask, bid)
            
            # 4. Check if any position closed -> nuclear reset
            if await self._check_nuclear_reset_trigger():
                return  # Reset triggered, exit
            
            # 5. Check for grid distance triggers across all sets
            await self._process_all_set_triggers(ask, bid)

    #grid distance trigger logic

    #TP/SL detection helpers (Same as old logic)

    def _update_touch_flags(self, ask: float, bid: float):
        """
        PRESERVED FROM ORIGINAL - Latch touch flags when price crosses TP/SL
        """
        for ticket, info in list(self.state.ticket_map.items()):
            if not info:
                continue

            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)

            # Skip positions that have not yet received TP/SL (e.g. center pair
            # opened without stops, waiting for 2nd entry alignment to fire)
            if tp_price == 0.0 and sl_price == 0.0:
                continue

            direction = info.get("direction", "")
            
            flags = self.state.ticket_touch_flags.get(ticket)
            if flags is None:
                flags = {"tp_touched": False, "sl_touched": False}
                self.state.ticket_touch_flags[ticket] = flags
            
            if direction == "buy":
                if not flags['tp_touched'] and bid >= tp_price:
                    flags['tp_touched'] = True
                if not flags['sl_touched'] and bid <= sl_price:
                    flags['sl_touched'] = True
            else:  # sell
                if not flags['tp_touched'] and ask <= tp_price:
                    flags['tp_touched'] = True
                if not flags['sl_touched'] and ask >= sl_price:
                    flags['sl_touched'] = True


    async def _check_position_drops(self, ask: float, bid: float):
        """
        PRESERVED FROM ORIGINAL - Detect positions closed by MT5 (TP/SL hit)
        
        NEW BEHAVIOR: Selective nuclear reset based on position_type
        - Custom singles (position_type='single_custom') close without triggering reset
        - Pair positions (position_type='pair') trigger nuclear reset
        """
        positions = mt5.positions_get(symbol=self.symbol)
        current_tickets = set()
        if positions:
            for pos in positions:
                current_tickets.add(pos.ticket)
        
        tracked_tickets = set(self.state.ticket_map.keys())
        dropped = tracked_tickets - current_tickets
        
        processed_groups = set()
        for ticket in dropped:
            info = self.state.ticket_map.get(ticket)
            if not info:
                continue

            group_id = info.get('split_group_id')
            # Handle split-group closure as a single event
            if group_id:
                if group_id in processed_groups:
                    continue
                processed_groups.add(group_id)

                group_tickets = list(self.state.split_group_map.get(group_id, []))
                # Compute realized pnl for all tickets in group (closed or will be closed)
                group_realized = 0.0
                any_pair = False
                for t in group_tickets:
                    tinfo = self.state.ticket_map.get(t)
                    if not tinfo:
                        continue
                    leg = tinfo.get('leg', '')
                    direction = tinfo.get('direction', '')
                    entry = tinfo.get('entry', 0)
                    tp_price = tinfo.get('tp', 0)
                    sl_price = tinfo.get('sl', 0)
                    lot = tinfo.get('lot', 0)
                    position_type = tinfo.get('position_type', 'pair')
                    any_pair = any_pair or (position_type == 'pair')

                    # Determine TP/SL using touch flags when possible
                    flags = self.state.ticket_touch_flags.get(t, {})
                    is_tp = flags.get('tp_touched', False)
                    is_sl = flags.get('sl_touched', False)
                    if not is_tp and not is_sl:
                        check_price = bid if direction == 'buy' else ask
                        tp_dist = abs(check_price - tp_price)
                        sl_dist = abs(check_price - sl_price)
                        is_tp = tp_dist < sl_dist
                        is_sl = not is_tp

                    close_price = tp_price if is_tp else sl_price
                    if direction == 'buy':
                        group_realized += (close_price - entry) * lot
                    else:
                        group_realized += (entry - close_price) * lot

                # Close any remaining open tickets in the group
                for t in list(group_tickets):
                    if t in (current_tickets or set()):
                        # close via broker
                        try:
                            self._close_position(t)
                        except Exception:
                            self.activity_log.log_error(f"Failed to close split-group ticket {t}")
                # Log as single event
                self.state.realized_pnl += group_realized
                group_set_index = info.get('set_index', 0)
                if any_pair:
                    self.activity_log.log_sl_hit(ticket, info.get('leg', ''), 0.0, group_realized, triggered_reset=True, set_index=group_set_index)
                    self._position_drop_detected_set_indices.add(group_set_index)
                else:
                    self.activity_log.log_sl_hit(ticket, info.get('leg', ''), 0.0, group_realized, triggered_reset=False, set_index=group_set_index)

                # Remove all tickets in group from tracking
                for t in list(group_tickets):
                    self._remove_ticket_from_all_levels(t)
                    if self.repository is not None:
                        await self.repository.delete_ticket(t)
                    self.state.total_positions = max(0, self.state.total_positions - 1)
                continue

            # Non-split ticket (original logic)
            leg = info.get("leg", "")
            direction = info.get("direction", "")
            entry = info.get("entry", 0)
            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)
            lot = info.get("lot", 0)
            position_type = info.get("position_type", "pair")  # Default to 'pair' for safety

            # Determine TP or SL using touch flags
            flags = self.state.ticket_touch_flags.get(ticket, {})
            is_tp = flags.get("tp_touched", False)
            is_sl = flags.get("sl_touched", False)

            # Fallback inference
            if not is_tp and not is_sl:
                check_price = bid if direction == "buy" else ask
                tp_dist = abs(check_price - tp_price)
                sl_dist = abs(check_price - sl_price)
                is_tp = tp_dist < sl_dist
                is_sl = not is_tp

            # Calculate PnL
            close_price = tp_price if is_tp else sl_price
            if direction == "buy":
                realized = (close_price - entry) * lot
            else:
                realized = (entry - close_price) * lot

            self.state.realized_pnl += realized

            # Determine if this closure triggers reset
            triggers_reset = (position_type == 'pair')
            set_index = info.get('set_index', 0)

            # Log with reset trigger indicator
            if is_tp:
                self.activity_log.log_tp_hit(ticket, leg, close_price, realized, "", triggered_reset=triggers_reset, set_index=set_index)
            else:
                self.activity_log.log_sl_hit(ticket, leg, close_price, realized, triggered_reset=triggers_reset, set_index=set_index)

            # Remove from tracking
            self._remove_ticket_from_all_levels(ticket)
            if self.repository is not None:
                await self.repository.delete_ticket(ticket)

            # Decrement total (for both pair and custom singles)
            self.state.total_positions -= 1

            # Set reset flag ONLY for pair positions
            if triggers_reset:
                self._position_drop_detected_set_indices.add(set_index)
        
        if dropped:
            await self.save_state()

    # Nuclear reset check (SAME but modified for 2-level logic)

    async def _check_nuclear_reset_trigger(self) -> bool:
        """
        Check if ANY position was closed (TP or SL hit)
        If yes -> trigger nuclear reset
        
        Returns True if reset was triggered
        """
        # If any position dropped, _check_position_drops already handled logging
        # Now we just check if total_positions decreased
        
        if self._position_drop_detected_set_indices:
            affected_sets = sorted(self._position_drop_detected_set_indices)
            self._position_drop_detected_set_indices.clear()
            self.activity_log.log_info(
                f"Position closed via TP/SL - triggering nuclear reset for sets {affected_sets}"
            )
            for set_index in affected_sets:
                await self._nuclear_reset_set(set_index, "TP_SL_HIT")
            return True
        
        return False


    async def _nuclear_reset_set(self, set_index: int, reason: str):
        """
        Reset only one set and restart it from the current market price.
        """
        self._ensure_set_state(set_index)
        old_cycle = self.state.cycle_count
        current_price = self.current_price

        print(f"[RESET] {self.symbol}: Set {set_index + 1} reset. Reason: {reason}")
        self.activity_log.log_reset(old_cycle, old_cycle + 1, reason, self.state.realized_pnl, set_index=set_index)

        # Close only pair positions that belong to this set.
        set_tickets = [
            ticket for ticket in self._get_tickets_for_set(set_index)
            if self.state.ticket_map.get(ticket, {}).get('position_type', 'pair') == 'pair'
        ]
        closed_count = 0
        for ticket in set_tickets:
            if self._close_position(ticket):
                closed_count += 1
                self._remove_ticket_from_all_levels(ticket)
                if self.repository is not None:
                    await self.repository.delete_ticket(ticket)

        if closed_count:
            self.state.total_positions = max(0, self.state.total_positions - closed_count)

        # Fresh set state and immediate restart at current market price.
        new_set_state = SetState(set_index=set_index)
        self.state.sets[set_index] = new_set_state
        await self._open_center_pair_for_set(set_index, current_price, log_start=True)
        self._sync_legacy_state_from_set(set_index)
        await self.save_state()


    def _reset_state(self):
        """Reset state to defaults (except cycle_count)"""
        cycle = self.state.cycle_count
        self.state = StrategyState()
        self.state.cycle_count = cycle
        self._position_drop_detected_set_indices.clear()
        self.orphan_tickets = []


    #Helper methods for order execution, position closing, and tracking management (SAME as old logic but adapted for new state structure)

    def _remove_ticket_from_tracking(self, ticket: int, grid_level: GridLevel):
        """Remove ticket from a specific grid level"""
        if ticket in grid_level.positions:
            del grid_level.positions[ticket]
        if ticket in self.state.ticket_map:
            del self.state.ticket_map[ticket]
        if ticket in self.state.ticket_touch_flags:
            del self.state.ticket_touch_flags[ticket]


    def _remove_ticket_from_all_levels(self, ticket: int):
        """Remove ticket from all grid levels and global tracking
        
        Position counter logic:
        - Pair positions: decrement position_counter (counts toward max_positions)
        - Custom single positions: DO NOT decrement position_counter (user requirement)
        - Center positions: always keep position_counter as-is
        """
        info = self.state.ticket_map.get(ticket)
        group_id = info.get('split_group_id') if info else None
        set_index = info.get('set_index', 0) if info else 0
        set_state = self._ensure_set_state(set_index) if self.state.sets else None

        # Remove from any level containers
        if set_state:
            if set_state.grid_level_1 and ticket in set_state.grid_level_1.positions:
                del set_state.grid_level_1.positions[ticket]
            if set_state.grid_level_2 and ticket in set_state.grid_level_2.positions:
                del set_state.grid_level_2.positions[ticket]

        # Remove from ticket tracking
        if ticket in self.state.ticket_map:
            del self.state.ticket_map[ticket]
        if ticket in self.state.ticket_touch_flags:
            del self.state.ticket_touch_flags[ticket]

        # If ticket belonged to a split group, only decrement position_counter when the last
        # ticket of the group is removed. Otherwise follow existing logic.
        if group_id:
            lst = self.state.split_group_map.get(group_id, [])
            if ticket in lst:
                try:
                    lst.remove(ticket)
                except ValueError:
                    pass
            if not lst:
                # last ticket removed -> decrement once for pair groups
                if info:
                    position_type = info.get('position_type', 'pair')
                    if position_type == 'pair' and set_state and set_state.position_counter > 0:
                        set_state.position_counter -= 1
                # cleanup map
                if group_id in self.state.split_group_map:
                    del self.state.split_group_map[group_id]
            else:
                # update stored list
                self.state.split_group_map[group_id] = lst
            return

        # Fallback: Only decrement position_counter for pair positions (not center, not custom singles)
        if info:
            leg = info.get("leg", "")
            position_type = info.get("position_type", "pair")
            # Center positions don't decrement position_counter
            if leg in {"CenterBuy", "CenterSell"}:
                pass  # Do nothing
            # Pair positions decrement position_counter
            elif position_type == "pair" and set_state and set_state.position_counter > 0:
                set_state.position_counter -= 1
            # Custom single positions DO NOT decrement position_counter (per user requirement)


    def _init_touch_flags(self, ticket: int):
        """Initialize touch flags for a new ticket"""
        self.state.ticket_touch_flags[ticket] = {
            "tp_touched": False,
            "sl_touched": False
        }


    def _close_position(self, ticket: int) -> bool:
        """
        PRESERVED FROM ORIGINAL - Close a single MT5 position
        """
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False
        
        pos = positions[0]
        tick = mt5.symbol_info_tick(self.symbol)
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
            "symbol": self.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": close_price,
            "deviation": 50,
            "magic": self.MAGIC_NUMBER,
            "comment": "close",
            "type_filling": mt5.ORDER_FILLING_FOK
        }
        
        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


    async def _execute_market_order(self, direction: str, lot_size: float,
                                    leg_name: str, target_price: float,
                                    tp_pips_override: Optional[float] = None,
                                    sl_pips_override: Optional[float] = None,
                                    skip_tp_sl: bool = False) -> Tuple[int, float, float, float]:
        """
        PRESERVED FROM ORIGINAL (with minor modifications)
        Send market order to MT5, returns (ticket, entry_price, tp_price, sl_price)
        """
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.activity_log.log_error(f"No tick for {leg_name}")
            return 0, 0.0, 0.0, 0.0
        
        # Determine execution parameters
        if direction == "buy":
            exec_price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
            check_price = tick.bid
        else:
            exec_price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL
            check_price = tick.ask

        if skip_tp_sl:
            tp = 0.0
            sl = 0.0
        else:
            # use pip offsets (relative distances) rather than absolute overrides
            tp_pips = tp_pips_override if tp_pips_override is not None else self.tp_pips
            sl_pips = sl_pips_override if sl_pips_override is not None else self.sl_pips
            if direction == "buy":
                tp = exec_price + float(tp_pips)
                sl = exec_price - float(sl_pips)
            else:
                tp = exec_price - float(tp_pips)
                sl = exec_price + float(sl_pips)
        
        if not skip_tp_sl:
            # Stops level safety
            symbol_info = mt5.symbol_info(self.symbol)
            if symbol_info:
                point = symbol_info.point
                stops_level = max(symbol_info.trade_stops_level, 10)
                min_dist = stops_level * point
                
                if direction == "buy":
                    if sl > check_price - min_dist:
                        sl = check_price - min_dist
                    if tp < check_price + min_dist:
                        tp = check_price + min_dist
                else:
                    if sl < check_price + min_dist:
                        sl = check_price + min_dist
                    if tp > check_price - min_dist:
                        tp = check_price - min_dist
        
        # Snapshot existing tickets
        positions_before = mt5.positions_get(symbol=self.symbol)
        existing_tickets = set(pos.ticket for pos in positions_before) if positions_before else set()
        
        # Send order
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": exec_price,
            "magic": self.MAGIC_NUMBER,
            "comment": f"{leg_name} C{self.state.cycle_count}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "deviation": 200
        }
        if not skip_tp_sl:
            request["sl"] = float(sl)
            request["tp"] = float(tp)
        
        result = mt5.order_send(request)
        
        # Handle MT5 response; on invalid stops, retry once using hardcoded per-asset stop level
        if result is None:
            error = mt5.last_error()
            self.activity_log.log_error(f"{leg_name} order failed: {error}")
            return 0, 0.0, 0.0, 0.0

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            invalid_code = getattr(mt5, 'TRADE_RETCODE_INVALID_STOPS', 10016)
            if result.retcode == invalid_code:
                stop_pips = MIN_STOP_PIPS_PER_ASSET.get(self.symbol, 10)
                symbol_info = mt5.symbol_info(self.symbol)
                point = symbol_info.point if symbol_info else 1.0
                fresh_tick = mt5.symbol_info_tick(self.symbol)
                retry_exec_price = (fresh_tick.ask if direction == 'buy' else fresh_tick.bid) if fresh_tick else exec_price
                if direction == 'buy':
                    new_sl = retry_exec_price - float(stop_pips) * point
                else:
                    new_sl = retry_exec_price + float(stop_pips) * point

                if symbol_info and not skip_tp_sl:
                    stops_level = max(symbol_info.trade_stops_level, 10)
                    min_dist = stops_level * point
                    check_price = (fresh_tick.bid if direction == 'buy' else fresh_tick.ask) if fresh_tick else (tick.bid if direction == 'buy' else tick.ask)
                    if direction == 'buy':
                        if new_sl > check_price - min_dist:
                            new_sl = check_price - min_dist
                    else:
                        if new_sl < check_price + min_dist:
                            new_sl = check_price + min_dist

                retry_req = dict(request)
                retry_req['price'] = float(retry_exec_price)
                if not skip_tp_sl:
                    retry_req['sl'] = float(new_sl)
                    retry_req['tp'] = float(tp)

                retry_res = mt5.order_send(retry_req)
                if retry_res and retry_res.retcode == mt5.TRADE_RETCODE_DONE:
                    self.activity_log.log_info(f"{leg_name}: Order retried with hardcoded stop level ({stop_pips} pips) and succeeded")
                    result = retry_res
                else:
                    err = retry_res.comment if retry_res else mt5.last_error()
                    self.activity_log.log_error(f"{leg_name} order failed after retry: {err}")
                    return 0, 0.0, 0.0, 0.0
            else:
                error = result.comment
                self.activity_log.log_error(f"{leg_name} order failed: {error}")
                return 0, 0.0, 0.0, 0.0
        
        ticket = result.order
        
        # Wait for position to appear
        await asyncio.sleep(0.1)
        
        # Find new position
        positions_after = mt5.positions_get(symbol=self.symbol)
        actual_entry = exec_price
        actual_ticket = ticket
        
        if positions_after:
            for pos in positions_after:
                if pos.ticket not in existing_tickets:
                    actual_ticket = pos.ticket
                    actual_entry = pos.price_open
                    break
            else:
                for pos in positions_after:
                    if pos.ticket == ticket:
                        actual_ticket = pos.ticket
                        actual_entry = pos.price_open
                        break
        
        # Return the actual ticket, actual entry price, and final TP/SL used (post-clamp)
        return actual_ticket, actual_entry, float(tp), float(sl)


    async def _split_and_execute_orders(self, direction: str, lot_size: float,
                                       leg_name: str, target_price: float,
                                       tp_pips_override: Optional[float] = None,
                                       sl_pips_override: Optional[float] = None,
                                       skip_tp_sl: bool = False) -> List[Tuple[int, float, float, float]]:
        """
        Split large lots into multiple orders not exceeding MAX_LOT_PER_ASSET and execute sequentially.
        Returns list of (ticket, entry, tp, sl) tuples in call order.
        """
        max_lot = MAX_LOT_PER_ASSET.get(self.symbol, 100)
        if lot_size <= max_lot:
            res = await self._execute_market_order(direction, lot_size, leg_name, target_price, tp_pips_override, sl_pips_override, skip_tp_sl)
            return [res]

        remaining = float(lot_size)
        chunks = []
        while remaining > 0 and len(chunks) < 20:
            chunk = min(remaining, float(max_lot))
            chunks.append(chunk)
            remaining -= chunk

        results = []
        for chunk in chunks:
            res = await self._execute_market_order(direction, chunk, leg_name, target_price, tp_pips_override, sl_pips_override, skip_tp_sl)
            results.append(res)

        return results

    def _adjusted_distance(self, price_a: float, price_b: float) -> float:
        return max(0.0, abs(price_a - price_b) - (self._last_known_spread / 2))

    async def _check_volatility_slippage(self, ask: float, bid: float):
        factor = self.volatility_tolerance_factor
        if factor is None:
            return
        mid = (ask + bid) / 2
        threshold = float(self.grid_distance) * float(factor)
        reset_sets: List[int] = []

        for set_index, set_state in enumerate(self.state.sets):
            if set_state.phase not in {"SINGLE_LEVEL", "TWO_LEVELS"}:
                continue

            nearest_level_price = self._get_nearest_level_price_for_set(set_index, mid)
            adjusted_distance = self._adjusted_distance(mid, nearest_level_price)
            if adjusted_distance >= threshold:
                self.activity_log.log_info(
                    f"VOLATILITY RESET: Adjusted distance {adjusted_distance:.5f} from nearest level {nearest_level_price:.5f} "
                    f"(spread deduction: {self._last_known_spread / 2:.5f}) exceeds {factor}x threshold {threshold:.5f}. Triggering nuclear reset.",
                    set_index=set_index,
                )
                reset_sets.append(set_index)

        for set_index in reset_sets:
            self._position_drop_detected_set_indices.discard(set_index)
            await self._nuclear_reset_set(set_index, "VOLATILITY_RESET")

    def _get_nearest_level_price_for_set(self, set_index: int, mid: float) -> float:
        set_state = self._ensure_set_state(set_index)
        if set_state.grid_level_2 and set_state.grid_level_2.active:
            p1 = set_state.grid_level_1.price if set_state.grid_level_1 else self.state.center_price
            p2 = set_state.grid_level_2.price
            return p1 if abs(mid - p1) < abs(mid - p2) else p2
        if set_state.grid_level_1:
            return set_state.grid_level_1.price
        return self.state.center_price

    async def _add_tp_sl_to_position(self, ticket: int, direction: str, entry_price: float) -> Tuple[bool, float, float]:
        """
        Add TP/SL to an existing position that was opened without stops.

        Returns (success, tp_price, sl_price). If MT5 rejects the modification,
        the caller should treat the returned values as virtual stops.
        """
        if direction == "buy":
            tp = entry_price + self.tp_pips
            sl = entry_price - self.sl_pips
        else:
            tp = entry_price - self.tp_pips
            sl = entry_price + self.sl_pips

        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.activity_log.log_error(f"Cannot modify position {ticket}: no tick data")
            return False, float(tp), float(sl)

        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info:
            point = symbol_info.point
            stops_level = max(symbol_info.trade_stops_level, 10)
            min_dist = stops_level * point
            check_price = tick.bid if direction == "buy" else tick.ask

            if direction == "buy":
                if sl > check_price - min_dist:
                    sl = check_price - min_dist
                if tp < check_price + min_dist:
                    tp = check_price + min_dist
            else:
                if sl < check_price + min_dist:
                    sl = check_price + min_dist
                if tp > check_price - min_dist:
                    tp = check_price - min_dist

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": ticket,
            "sl": float(sl),
            "tp": float(tp),
        }

        grid_level = None
        position_type = "pair"
        ticket_info = self.state.ticket_map.get(ticket, {})
        set_index = ticket_info.get("set_index", 0)
        set_state = self._ensure_set_state(set_index)
        if set_state.grid_level_1 and ticket in set_state.grid_level_1.positions:
            grid_level = set_state.grid_level_1
            position_type = grid_level.positions[ticket].get("position_type", "pair")
        elif set_state.grid_level_2 and ticket in set_state.grid_level_2.positions:
            grid_level = set_state.grid_level_2
            position_type = set_state.grid_level_2.positions[ticket].get("position_type", "pair")

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.activity_log.log_info(
                f"Added TP/SL to position {ticket}: TP={tp:.5f}, SL={sl:.5f}"
            )
            if grid_level:
                pos_type = "single_custom" if position_type == "single_custom" else "pair"
                if pos_type == "pair":
                    if direction == "buy":
                        grid_level.reference_buy_tp = float(tp)
                        grid_level.reference_buy_sl = float(sl)
                    else:
                        grid_level.reference_sell_tp = float(tp)
                        grid_level.reference_sell_sl = float(sl)
                else:
                    if direction == "buy":
                        grid_level.reference_custom_buy_tp = float(tp)
                        grid_level.reference_custom_buy_sl = float(sl)
                    else:
                        grid_level.reference_custom_sell_tp = float(tp)
                        grid_level.reference_custom_sell_sl = float(sl)

                self.activity_log.log_info(
                    f"Center {direction.upper()} set as {pos_type} reference at level {grid_level.price:.5f}"
                )
            return True, float(tp), float(sl)

        error = result.comment if result else mt5.last_error()
        self.activity_log.log_info(
            f"Broker rejected TP/SL for position {ticket} ({error}). Using virtual TP/SL: TP={tp:.5f}, SL={sl:.5f}"
        )
        if grid_level:
            aligned_tp, aligned_sl, _ = await self._align_position_tp_sl(
                ticket,
                direction,
                float(tp),
                float(sl),
                grid_level,
                position_type,
                has_virtual_stops=True,
            )
            return False, float(aligned_tp), float(aligned_sl)
        return False, float(tp), float(sl)

    async def _align_position_tp_sl(
        self,
        ticket: int,
        direction: str,
        tp: float,
        sl: float,
        grid_level: Optional[GridLevel],
        position_type: str,
        has_virtual_stops: bool = False,
    ) -> Tuple[float, float, bool]:
        """
        Keep the in-memory level state aligned with the TP/SL values used for a position.
        When the broker rejects stops, the same values are stored as virtual stops so the
        manual stop checker can close the position later.
        """
        if grid_level is None:
            return float(tp), float(sl), False

        info = grid_level.positions.get(ticket)
        if not info:
            return float(tp), float(sl), False

        tp = float(tp)
        sl = float(sl)
        info["tp"] = tp
        info["sl"] = sl
        info["has_virtual_stops"] = has_virtual_stops
        self.state.ticket_map[ticket] = info

        if position_type == "single_custom":
            if direction == "buy":
                grid_level.reference_custom_buy_tp = tp
                grid_level.reference_custom_buy_sl = sl
            else:
                grid_level.reference_custom_sell_tp = tp
                grid_level.reference_custom_sell_sl = sl
        else:
            if direction == "buy":
                grid_level.reference_buy_tp = tp
                grid_level.reference_buy_sl = sl
            else:
                grid_level.reference_sell_tp = tp
                grid_level.reference_sell_sl = sl

        return tp, sl, True

    async def _check_virtual_stops(self, ask: float, bid: float):
        """Close positions manually when virtual TP/SL thresholds are hit."""
        positions_to_close = []

        for ticket, info in list(self.state.ticket_map.items()):
            if not info or not info.get("has_virtual_stops", False):
                continue

            direction = info.get("direction", "")
            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)

            if direction == "buy":
                check_price = bid
                if tp_price > 0 and check_price >= tp_price:
                    positions_to_close.append((ticket, "tp", tp_price, check_price))
                elif sl_price > 0 and check_price <= sl_price:
                    positions_to_close.append((ticket, "sl", sl_price, check_price))
            else:
                check_price = ask
                if tp_price > 0 and check_price <= tp_price:
                    positions_to_close.append((ticket, "tp", tp_price, check_price))
                elif sl_price > 0 and check_price >= sl_price:
                    positions_to_close.append((ticket, "sl", sl_price, check_price))

        for ticket, hit_type, target_price, actual_price in positions_to_close:
            info = self.state.ticket_map.get(ticket)
            if not info:
                continue

            if not self._close_position(ticket):
                self.activity_log.log_error(f"Failed to close virtual-stop position {ticket}")
                continue

            leg = info.get("leg", "")
            direction = info.get("direction", "")
            entry = info.get("entry", 0)
            lot = info.get("lot", 0)
            position_type = info.get("position_type", "pair")

            if direction == "buy":
                realized = (actual_price - entry) * lot
            else:
                realized = (entry - actual_price) * lot

            self.state.realized_pnl += realized
            triggers_reset = position_type == "pair"

            if hit_type == "tp":
                self.activity_log.log_tp_hit(
                    ticket,
                    leg,
                    target_price,
                    realized,
                    action="(virtual TP)",
                    triggered_reset=triggers_reset,
                )
            else:
                self.activity_log.log_sl_hit(
                    ticket,
                    leg,
                    target_price,
                    realized,
                    action="(virtual SL)",
                    triggered_reset=triggers_reset,
                )

            self._remove_ticket_from_all_levels(ticket)
            self.state.total_positions -= 1
            if triggers_reset:
                self._position_drop_detected_set_indices.add(info.get('set_index', 0))

        if positions_to_close:
            await self.save_state()

    async def save_state(self):
        """Persist the current strategy state."""
        if self.repository is None:
            self.repository = Repository(self.symbol)
            await self.repository.initialize()

        def _serialize_grid_level(level: Optional[GridLevel]) -> Dict[str, Any]:
            if not level:
                return {}
            return {
                "price": level.price,
                "active": level.active,
                "reference_buy_tp": level.reference_buy_tp,
                "reference_buy_sl": level.reference_buy_sl,
                "reference_sell_tp": level.reference_sell_tp,
                "reference_sell_sl": level.reference_sell_sl,
                "reference_custom_buy_tp": level.reference_custom_buy_tp,
                "reference_custom_buy_sl": level.reference_custom_buy_sl,
                "reference_custom_sell_tp": level.reference_custom_sell_tp,
                "reference_custom_sell_sl": level.reference_custom_sell_sl,
                "positions": level.positions,
            }

        serialized_sets = []
        for set_state in self.state.sets:
            serialized_sets.append({
                "set_index": set_state.set_index,
                "phase": set_state.phase,
                "grid_level_1_price": set_state.grid_level_1.price if set_state.grid_level_1 else 0.0,
                "grid_level_2_price": set_state.grid_level_2.price if set_state.grid_level_2 else 0.0,
                "position_counter": set_state.position_counter,
                "last_move_direction": set_state.last_move_direction,
                "is_final_group_reached": set_state.is_final_group_reached,
            })

        metadata = json.dumps(
            {
                "phase": self.state.phase,
                "center_price": self.state.center_price,
                "total_positions": self.state.total_positions,
                "realized_pnl": self.state.realized_pnl,
                "sets": serialized_sets,
            }
        )

        await self.repository.save_state(
            phase=self.state.phase,
            center_price=self.state.center_price,
            iteration=self.state.cycle_count,
            cycle_id=self.state.cycle_count,
            anchor_price=self.state.center_price,
            metadata=metadata,
        )

    async def reconcile_on_startup(self) -> dict:
        """Best-effort reconcile of DB state against live MT5 positions."""
        if self.repository is None:
            self.repository = Repository(self.symbol)
            await self.repository.initialize()

        summary = {
            "recovered_sets": [],
            "offline_closures": [],
            "orphans": [],
        }

        state_row = await self.repository.get_state()
        metadata = {}
        if state_row.get("metadata"):
            try:
                metadata = json.loads(state_row["metadata"])
            except Exception:
                metadata = {}

        self._reset_state()
        self.state.center_price = float(metadata.get("center_price", state_row.get("center_price", 0.0) or 0.0))
        self.state.phase = metadata.get("phase", state_row.get("phase", "IDLE"))
        self.state.cycle_count = int(state_row.get("cycle_id", 0) or 0)
        self.state.realized_pnl = float(metadata.get("realized_pnl", 0.0) or 0.0)
        self.state.total_positions = 0

        set_entries = metadata.get("sets", []) if isinstance(metadata.get("sets", []), list) else []
        for entry in set_entries:
            if not isinstance(entry, dict):
                continue
            set_index = int(entry.get("set_index", 0))
            set_state = self._ensure_set_state(set_index)
            set_state.phase = entry.get("phase", "IDLE")
            set_state.position_counter = int(entry.get("position_counter", 0) or 0)
            set_state.last_move_direction = entry.get("last_move_direction", "")
            set_state.is_final_group_reached = bool(entry.get("is_final_group_reached", False))
            level_1_price = float(entry.get("grid_level_1_price", 0.0) or 0.0)
            level_2_price = float(entry.get("grid_level_2_price", 0.0) or 0.0)
            set_state.grid_level_1 = GridLevel(price=level_1_price, active=level_1_price > 0)
            set_state.grid_level_2 = GridLevel(price=level_2_price, active=level_2_price > 0) if level_2_price > 0 else None

        db_ticket_map = await self.repository.get_ticket_map()
        live_positions = mt5.positions_get(symbol=self.symbol) or []
        live_by_ticket = {pos.ticket: pos for pos in live_positions}

        offline_pair_reset_sets = set()

        for ticket, row in db_ticket_map.items():
            set_index, pair_index, grid_level_index, leg, entry_price, tp_price, sl_price = row
            if ticket in live_by_ticket:
                set_state = self._ensure_set_state(set_index)
                if grid_level_index not in (1, 2):
                    grid_level_index = 1 if set_state.grid_level_1 else 2 if set_state.grid_level_2 else 1
                grid_level = set_state.grid_level_1 if grid_level_index == 1 else set_state.grid_level_2
                if grid_level is None:
                    if grid_level_index == 2:
                        grid_level = GridLevel(price=self.state.center_price, active=True)
                        set_state.grid_level_2 = grid_level
                    else:
                        grid_level = GridLevel(price=self.state.center_price, active=True)
                        set_state.grid_level_1 = grid_level

                info = {
                    "leg": leg,
                    "direction": live_by_ticket[ticket].type == mt5.ORDER_TYPE_BUY and "buy" or "sell",
                    "entry": entry_price,
                    "tp": tp_price,
                    "sl": sl_price,
                    "lot": getattr(live_by_ticket[ticket], "volume", 0.0),
                    "position_type": "pair" if "single" not in leg.lower() else "single_custom",
                    "set_index": set_index,
                }
                grid_level.positions[ticket] = info
                self.state.ticket_map[ticket] = info
                self._init_touch_flags(ticket)
                self.state.total_positions += 1
                if set_index not in summary["recovered_sets"]:
                    summary["recovered_sets"].append(set_index)
                await self.repository.save_ticket(
                    ticket=ticket,
                    cycle_id=self.state.cycle_count,
                    pair_index=pair_index,
                    leg=leg,
                    trade_count=0,
                    entry_price=entry_price,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    set_index=set_index,
                    grid_level=grid_level_index,
                )
            else:
                set_index = int(set_index)
                summary["offline_closures"].append({"ticket": ticket, "leg": leg, "set_index": set_index})
                self.activity_log.log_info(
                    f"[Set {set_index + 1}] Ticket {ticket} ({leg}) not found on restart — assumed closed while offline, treating as SL-equivalent"
                )
                self.activity_log.log_info(
                    "PnL not recoverable for offline closure — running total for this cycle is incomplete",
                    set_index=set_index,
                )
                if self.repository is not None:
                    await self.repository.delete_ticket(ticket)
                if "single" not in leg.lower():
                    offline_pair_reset_sets.add(set_index)

        for set_index in sorted(offline_pair_reset_sets):
            await self._nuclear_reset_set(set_index, "OFFLINE_RECOVERY")
            if set_index not in summary["recovered_sets"]:
                summary["recovered_sets"].append(set_index)

        for pos in live_positions:
            if pos.ticket not in db_ticket_map:
                self.orphan_tickets.append(pos.ticket)
                summary["orphans"].append(pos.ticket)
                self.activity_log.log_info(
                    f"[ORPHAN] Ticket {pos.ticket} on {self.symbol} open in MT5 but untracked after recovery — not managed by any set. Manual review needed."
                )

        self.running = bool(live_positions or offline_pair_reset_sets)
        self.activity_log.log_info(f"[RECOVERY] {summary}")
        await self.save_state()
        return summary


    #Graceful stop and position terminate (same as old logic)

    async def stop(self):
        """
        PRESERVED FROM ORIGINAL
        Graceful stop - complete current cycle before stopping
        """
        if not self.running:
            return
        
        print(f"[STOP] {self.symbol}: Graceful stop initiated.")
        self.graceful_stop = True
        self.activity_log.log_graceful_stop(self.state.cycle_count, "manual/timeout")
        
        # If idle or no positions, stop immediately
        if self.state.phase == "IDLE" or self.state.total_positions == 0:
            self.running = False
            self.activity_log.log_stop(self.state.cycle_count, "graceful_stop_immediate")
            await self.save_state()
            print(f"[STOP] {self.symbol}: Stopped immediately (no positions).")


    async def terminate(self):
        """
        PRESERVED FROM ORIGINAL
        Nuclear reset - close ALL positions immediately, don't restart
        """
        print(f"[TERMINATE] {self.symbol}: Closing ALL positions...")
        self.activity_log.log_info("TERMINATE: Closing all positions...")
        
        # Close all positions
        positions = mt5.positions_get(symbol=self.symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                if self._close_position(pos.ticket):
                    closed_count += 1
        
        print(f"[TERMINATE] {self.symbol}: Closed {closed_count} positions.")
        self.activity_log.log_info(f"TERMINATE: Closed {closed_count} positions")
        if self.repository is not None:
            await self.repository.clear_ticket_map()
        
        # Full reset
        self._reset_state()
        self.running = False
        self.graceful_stop = False
        self.state.phase = "IDLE"
        self.state.cycle_count = 0
        
        await self.save_state()
        print(f"[TERMINATE] {self.symbol}: Terminated completely.")

    async def close(self):
        """Release persistent resources held by the strategy."""
        if self.repository is not None:
            await self.repository.close()
            self.repository = None


    #Status API

    def get_status(self) -> dict:
        """
        PRESERVED FROM ORIGINAL (with field updates)
        Return status dict for API polling
        """
        per_set = []
        for set_state in self.state.sets:
            level_1_open = len(set_state.grid_level_1.positions) if set_state.grid_level_1 else 0
            level_2_open = len(set_state.grid_level_2.positions) if set_state.grid_level_2 else 0
            per_set.append({
                "set_index": set_state.set_index,
                "phase": set_state.phase,
                "open_positions": level_1_open + level_2_open,
                "position_counter": set_state.position_counter,
                "last_move_direction": set_state.last_move_direction,
                "is_final_group_reached": set_state.is_final_group_reached,
                "grid_level_1_price": set_state.grid_level_1.price if set_state.grid_level_1 else 0,
                "grid_level_2_price": set_state.grid_level_2.price if set_state.grid_level_2 else 0,
            })
        return {
            "running": self.running,
            "phase": self.state.phase,
            "cycle_count": self.state.cycle_count,
            "center_price": self.state.center_price,
            "open_positions": self.state.total_positions,
            "max_positions": self.max_positions,
            "realized_pnl": self.state.realized_pnl,
            "graceful_stop": self.graceful_stop,
            "is_resetting": self.state.phase == "RESETTING",
            "step": self.state.cycle_count,
            "iteration": self.state.cycle_count,
            "sets": per_set,
            "orphan_tickets": list(self.orphan_tickets),
        }
