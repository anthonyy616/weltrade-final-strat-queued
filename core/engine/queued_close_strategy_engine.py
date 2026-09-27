"""Queued Close Strategy Engine.

A pool of positions opens at startup; the rest of the cycle only ever closes
them down in a queued order. No bouncing, no nuclear reset on a single TP/SL.
Spec: agent/02-architecture.md (authoritative for names/shapes/formulas).
"""

from dataclasses import dataclass, field
from typing import Dict, List

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
