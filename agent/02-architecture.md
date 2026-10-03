# Queued Close Strategy — Architecture

This is the authoritative shape of the new fork. Treat every field and method name here as the name to actually use in code unless there's a strong reason to deviate — consistency with this document matters more than any individual naming preference, because this document is what ties the strategy write-up to the actual implementation.

## 1. Fork boundary

This is a separate fork: its own repository copy (branched from the Weltrade fork, since Weltrade's plain symbol handling — no account-type suffix — is what this strategy uses), its own `config_<user_id>.json` naming, its own SQLite DB file, its own `static/index.html`. It is not a mode switch inside the existing Weltrade fork. It shares code *patterns* with both existing forks (orchestrator, logging, persistence shape, tick loop) but runs as an independent process/deployment.

## 2. Symbol handling

Weltrade has no account-type suffix concept. `mt5_symbol` on the new engine should simply return `self.symbol` unmodified — do not port over Exness's `ACCOUNT_TYPE_SUFFIX` mapping, `symbol_suffix` property, or lot-splitting (`EXNESS_MAX_LOT`) logic. If lot-size splitting turns out to be needed for this broker, that is a separate decision to make explicitly later, not something to inherit by default.

## 3. Config schema (per symbol)

```json
{
  "enabled": false,
  "buy_count": 5,
  "sell_count": 5,
  "buy_lot": 0.01,
  "sell_lot": 0.01,
  "constant_side": "sell",
  "grid_distance": 50.0,
  "moving_freq": 10.0,
  "constant_freq": 8.0
}
```

- `constant_side` is `"buy"` or `"sell"` only — validate against this exact set, reject anything else. The moving side is always the other one; do not store it as a separate field (avoids the two fields ever disagreeing).
- `buy_count` / `sell_count`: positive integers, no upper bound imposed by the strategy itself, though a sane UI-level cap (e.g. 500) is reasonable to prevent fat-fingered input from opening an absurd number of positions.
- `constant_freq` must be strictly less than `moving_freq`. Reject or clamp on config update; this assumption is baked into the closing-target formulas and is not safe to relax without redoing the math.
- Global settings (`max_runtime_minutes`) stay exactly as they are in the current `ConfigManager` — no changes needed there.
- Remove entirely for this fork: `tp_pips`, `sl_pips`, `second_entry_*_pips`, `pair_buy_lots`/`pair_sell_lots`/`single_lots`, `max_positions`, `sets`/`sets_config`. None of these concepts exist in this strategy. Do not keep them "just in case" — their presence invites an agent or future maintainer to wire them into logic where they don't belong.

## 4. State model

Replace `GridLevel` / `StrategyState` entirely for this engine — do not attempt to subclass or partially reuse them, the shapes don't overlap enough to be worth it.

```python
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
    constant_tickets: List[int] = field(default_factory=list)   # open constant-side tickets, order not meaningful

    up_targets: List[ConstantTargetLevel] = field(default_factory=list)
    down_targets: List[ConstantTargetLevel] = field(default_factory=list)

    moving_total: int = 0
    constant_total: int = 0
    moving_closed_count: int = 0
    constant_closed_count: int = 0

    close_queue: List[QueuedClose] = field(default_factory=list)

    catching_up: bool = False   # True during reconnect reconciliation; gates live tick processing

    cycle_count: int = 0
    realized_pnl: float = 0.0
```

Naming note: `up_targets` and `down_targets` are deliberately two separate lists with no shared index space, exactly to avoid the overwrite risk flagged during design — a level fired from the up list and a level fired from the down list are never the same object or the same array slot, even if their prices happen to coincide numerically.

## 5. Startup sequence

1. Fetch current tick, set `center_price`, compute `grid_level_up = center + grid_distance * point` and `grid_level_down = center - grid_distance * point`.
2. Precompute `up_targets` and `down_targets`, each with `constant_total` entries (`constant_total` = whichever count belongs to the constant side).
3. Open all `buy_count` BUY positions and all `sell_count` SELL positions at market, at their respective configured lots, in a loop — no batching assumption, handle partial failures (see section 8).
4. For every moving-side position opened, assign it TP and SL from the appropriate slot in the up/down sequences (moving side's TP list and SL list — see formulas in section 6), one slot per position, no two positions sharing a slot.
5. Constant-side positions open with no TP/SL at all — do not send `sl`/`tp` fields in the order request for them, ever.
6. Set `phase = "ACTIVE"`, save state.

## 6. Closing-target formulas (implement exactly as specified — do not re-derive)

Let `diff = moving_freq - constant_freq`.

**Moving = BUY:**
- TP levels (n = 1..moving_total): `grid_level_up + n * moving_freq * point`
- SL levels (n = 1..moving_total): `grid_level_down - n * moving_freq * point`
- Constant target, up direction, slot n: `up_TP_level_n - diff * point`
- Constant target, down direction, slot n: `down_SL_level_n - diff * point`

**Moving = SELL:**
- TP levels (n = 1..moving_total): `grid_level_down - n * moving_freq * point`
- SL levels (n = 1..moving_total): `grid_level_up + n * moving_freq * point`
- Constant target, down direction, slot n: `down_TP_level_n + diff * point`
- Constant target, up direction, slot n: `up_SL_level_n + diff * point`

Sanity-check numbers (center=1000, grid_distance=50, moving_freq=10, constant_freq=8, diff=2, point=1 for this example):
- Moving=BUY: up TP levels 1060/1070/1080..., down SL levels 940/930/920... Constant targets: up-direction 1058/1068/1078..., down-direction 938/928/918...
- Moving=SELL: down TP levels 940/930/920..., up SL levels 1060/1070/1080... Constant targets: down-direction 942/932/922..., up-direction 1062/1072/1082...

If a coding agent's implementation produces different numbers than these for the same inputs, the implementation is wrong — use this block as a unit test fixture.

## 7. Tick handling

On every tick, if `catching_up` is `True`, skip all strategy logic (reconciliation owns processing during this window).

Otherwise, each tick:

1. Detect moving-position closures the same way the existing bot detects position drops — diff tracked tickets against `mt5.positions_get()`.
2. For each detected closure, in slot order if multiple closed in the same tick:
   a. Mark the `MovingPositionRecord` closed, increment `moving_closed_count`.
   b. Determine direction (up/down) and slot index from the record.
   c. Look up the corresponding `ConstantTargetLevel` in `up_targets` or `down_targets` and enqueue it as a `QueuedClose` if not already fired.
3. Process the queue: for every `QueuedClose` currently in the queue, attempt to close one open constant-side ticket at market. On success: mark `fired = True`, increment `constant_closed_count`, remove from queue, decrement open constant ticket tracking. On failure: increment `retry_count`, leave in queue (see section 8 for the terminate-all condition).
4. Check cycle-end: if `moving_closed_count >= moving_total` or `constant_closed_count >= constant_total`, force-close every remaining open position on both sides immediately (including anything still in the queue), log as early-completion if the counts weren't equal, log as normal completion if they were, then start a new cycle at current price.
5. If neither side is a reversal-price check is needed — reversal handling is implicit: whichever grid level (up or down) price actually reaches next simply has its precomputed list consulted, regardless of what happened on the other side earlier in the cycle. No special "reversal" branch should exist in code; if you find yourself writing one, the running-count model has been implemented incorrectly.

## 8. Order-level retry and terminate condition

Wrap constant-close order sends in their own retry path, separate from the MT5 connection-level reconnect logic in `trading_engine.py`. On a failed close attempt: leave the `QueuedClose` in the queue, increment its `retry_count`, try again on a subsequent tick (do not busy-loop within a single tick). If a retry fails **and** the queue would be empty after this item succeeds (i.e., this is the last pending item), instead of retrying again: terminate all positions for the symbol and start a new cycle. This bounds the failure mode to "a new cycle begins" rather than an indefinite stuck state.

## 9. Persistence (new repository tables)

Alongside the existing `symbol_state`-style row (reused for phase/cycle/center-price/timestamps), add:

- `moving_positions`: `ticket` (PK), `symbol`, `cycle_id`, `entry`, `tp_price`, `sl_price`, `direction`, `slot_index`, `closed`.
- `constant_targets`: `symbol`, `cycle_id`, `direction` (up/down), `slot_index`, `price`, `fired` — composite key on (`symbol`, `cycle_id`, `direction`, `slot_index`).
- `constant_queue`: `id` (autoincrement), `symbol`, `cycle_id`, `direction`, `slot_index`, `retry_count`, `enqueued_at` — one row per currently-pending queue entry, deleted on success.
- `constant_tickets`: `ticket` (PK), `symbol`, `cycle_id` — open constant-side tickets not yet bound to any specific target.

Save state after every mutation that would be expensive to redo from scratch on a crash: every moving closure, every queue release attempt (success or failure), every cycle-end.

## 10. Crash / reconnect reconciliation

Wire this into `trading_engine.py`'s reconnect path (the same place `_reconnect_mt5` succeeds), before the tick loop resumes normal processing:

1. Set `catching_up = True` on every affected symbol's engine.
2. For each symbol: `mt5.positions_get(symbol=mt5_symbol)` filtered to the strategy's magic number, collect the ticket set.
3. Diff against `moving_positions` and `constant_tickets` rows marked open in the DB. Anything in the DB as open but absent from MT5 closed while disconnected.
4. For each such ticket, call `mt5.history_deals_get()` scoped to that position/ticket to retrieve the actual close price and whether TP or SL was hit.
5. Replay each closure through the exact same function used for a live-tick closure (section 7, steps 2–4) — this guarantees identical queue-release and cycle-end behavior whether the closure was detected live or during catch-up.
6. Once every diffed ticket has been replayed and the queue is stable (either empty or genuinely waiting on a moving closure that hasn't happened yet), set `catching_up = False` and allow live ticks to resume.
7. If replay determines the cycle had already ended during the outage (one side's total reached), the force-close-and-restart in step 4 of section 7 runs exactly as it would live — there is no separate "recovered mid-force-close" branch.

## 11. Activity logging additions

New `LEG_NAMES` entries in `ActivityLogger`: `MovingBuy`, `MovingSell`, `ConstantBuy`, `ConstantSell`. Do not reuse `PairBuy`/`PairSell`/`SingleBuy`/`SingleSell`/`CenterBuy`/`CenterSell` — those names carry Grid Bounce–specific meaning and would be actively misleading in this fork's logs.

Add a distinct log call for cycle completion type — e.g. `log_cycle_complete(cycle, reason)` where `reason` is `"NORMAL"` (both sides reached total together) or `"EARLY_FORCE_CLOSE"` (one side ran out first, remainder force-closed). Do not fold this into the existing `log_reset` — it's a genuinely different event, not a TP/SL-triggered nuclear reset.

## 12. API and multi-user

`BotManager`, `get_or_create_bot`, per-user config file naming, and Supabase auth carry over unchanged — no reason for this strategy to behave differently across users or sessions than the existing forks. `api/server.py` route shapes stay the same; only the `SymbolConfig` Pydantic model's fields change to match section 3.

## 13. UI

Per-symbol panel fields, replacing the current lot/TP/SL/sets UI entirely: buy count, sell count, buy lot, sell lot, a `<select>` for constant side (options: Buy, Sell — no free-text input, no third option), moving closing frequency, constant closing frequency, grid distance.

Add a status readout beyond the existing single "positions" count: show moving closed/total and constant closed/total separately (e.g. "Moving 3/5 · Constant 2/3"), since a single combined number is much less informative for this strategy than for Grid Bounce. This is a genuinely new status concept, not a relabeling of an existing field — implement it as such rather than trying to force the old `open_positions` field to carry both meanings.
