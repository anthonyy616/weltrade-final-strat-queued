# Implementation Details — File by File

Reference documents: `agent/02-architecture.md` (authoritative shapes), `agent/04-coding-style-and-rules.md` (naming/rules), `agent/05-mistakes-and-race-conditions.md` (races). This doc maps each spec requirement to the code that implements it.

## 1. `core/engine/queued_close_strategy_engine.py` (new file)

### Pure formula functions (module level)

- `moving_tp_level(grid_up, grid_down, n, moving_freq, moving_side)` — agent/02 §6. Moving=BUY: `grid_up + n*freq`; moving=SELL: `grid_down - n*freq`.
- `moving_sl_level(...)` — the mirror pair of the TP formula.
- `constant_target(moving_level, diff, moving_side)` — constant target sits `diff` **behind** the moving level on the side already reached. Moving=BUY: `level - diff`; moving=SELL: `level + diff`.
- `compute_constant_targets(...)` — returns `(up_targets, down_targets)` as lists of `(price, slot_index)` tuples, `constant_total` entries each, 1-based slots.

Verified against the worked example (center=1000, grid=50, mf=10, cf=8): BUY up `1058/1068/1078`, down `938/928/918`; SELL down `942/932/922`, up `1062/1072/1082`. Exact match.

**Note on pip conversion:** per Anthony's decision these use raw price units (no `* point`), matching how the existing fork actually trades. See `00-build-summary.md`.

### State dataclasses (agent/02 §4)

`MovingPositionRecord`, `ConstantTargetLevel`, `QueuedClose`, `QueuedCloseState` — exact names and fields from the spec, with three additive runtime-only fields:

- `QueuedCloseState.moving_side / moving_freq / constant_freq / grid_distance` — the **config snapshot**. Captured once in `start()`; the entire tick path reads these from state, never from live config (agent/05 "config values must be snapshotted"). Enforced structurally: no `config.get(...)` calls exist in tick/queue/cycle-end code.
- `MovingPositionRecord.lot` and `QueuedCloseState.moving_lot / constant_lot` — needed for PnL display and pool opening; captured at start, persisted in symbol_state metadata for recovery.

`up_targets` and `down_targets` are two genuinely separate lists (agent/04 rule). `moving_total` etc. are stored on state after being derived once at start — this is the snapshot, not a duplicate of config.

### Engine class

- `MAGIC_NUMBER = 123456` (same as the old engine — positions remain identifiable across the fork swap).
- `mt5_symbol` property returns `self.symbol` (Weltrade, no suffix, agent/02 §2) — **all** broker calls go through it.
- `execution_lock: asyncio.Lock` — same pattern as the old engine.

## 2. Startup (`start`, `_start_new_cycle_at_market`)

Sequence per agent/02 §5:

1. Validate `constant_side` against `("buy", "sell")` — refuse to start on invalid value (no silent fallback).
2. Snapshot config: moving side derived (never stored as a separate config field), counts, lots, frequencies, grid distance.
3. Increment `cycle_count`, clear per-cycle in-memory lists (`_clear_cycle_state` — agent/05 petty-lists rule).
4. Fetch tick → `center_price`, `grid_level_up/down = center ± grid_distance`.
5. Precompute both target lists in full, persist to `constant_targets` table **before** opening anything.
6. Open moving positions one at a time, slot `n` = 1..moving_total, each with its own absolute TP/SL from the formulas. Failed order → logged, pool continues partially (no batching assumption).
7. Open constant positions with `tp_price=None, sl_price=None` — the order request dict **never receives** `sl`/`tp` keys for these. Comment at the call site marks this as deliberate (agent/04).
8. `phase = ACTIVE`, save symbol state.

Ticket identity: snapshot tickets before `order_send`, re-query `positions_get` after the 0.1s confirmation pause, match by exclusion, fall back to `result.order` (agent/05 MT5 ticket identity rule).

## 3. Tick handling (`on_external_tick` and helpers)

```
on_external_tick(ask, bid)
  └─ async with execution_lock:          # ONE continuous block per tick
       if catching_up: return            # check is INSIDE the lock (agent/05)
       if phase != ACTIVE: return
       _process_closures_and_queue()     # detection + queue drain
       _check_cycle_end()                # cycle-end check
       (graceful-stop stop check)
```

- **Detection**: diff tracked moving tickets vs `mt5.positions_get()`. Multiple closures sorted by `slot_index` before processing (agent/03 edge-case table).
- **`_handle_moving_closure(ticket)`** — THE single closure path. Marks closed, increments `moving_closed_count`, computes PnL, logs TP/SL hit, persists `closed=1`, then enqueues the paired constant target. **Both live ticks and reconciliation replay call this function** — no parallel implementation exists (agent/03).
- **Direction lookup**: TP fire → `up` for moving=BUY, `down` for moving=SELL (SL fire is the mirror). The lookup is keyed by `(direction, slot_index)`; there is no reversal branch anywhere — reversal handling is implicit by design (agent/03 step 5).
- **`_enqueue_constant_target`**: skips if already `fired` or already pending in the queue; persists a `constant_queue` row.
- **`_process_close_queue`**: iterates `list(self.close_queue)` (snapshot — agent/05 iterating-and-mutating rule). FIFO: `constant_tickets[0]` satisfies any entry (running-count model, no slot binding — agent/04). One close attempt per item per tick, never an internal retry loop.
  - Success: mark fired, increment `constant_closed_count`, pop ticket, remove queue item, persist all three (delete `constant_tickets` row, delete `constant_queue` row, set `constant_targets.fired=1`).
  - Failure: increment `retry_count`, persist via `bump_constant_queue_retry`. **Only if this item is the last one pending** (`len(close_queue) == 1`): `_end_cycle("TERMINATE_QUEUE_EXHAUSTED")`. Otherwise leave in queue for the next tick.
- **`_check_cycle_end`**: fires when either count reaches its total. `early = (moving_closed != moving_total) or (constant_closed != constant_total)` → `EARLY_FORCE_CLOSE` vs `NORMAL`.
- **`_end_cycle(reason)`**: sets `RESETTING` → force-closes everything (magic-filtered) → **explicitly clears** `constant_queue` + `constant_tickets` + `moving_positions` DB rows for the cycle (agent/04 stale-rows rule) → logs `log_cycle_complete` with the reason → if graceful_stop, stops; otherwise starts a new cycle at current market with the same snapshot values.

TP-vs-SL determination in `_handle_moving_closure`: prefers the broker's own deal record via `_extract_close_info` (exact); falls back to proximity inference against last tick if history is unavailable.

## 4. Persistence (`core/persistence/repository.py`)

New tables (agent/02 §9):

| Table | Key | Purpose |
|---|---|---|
| `moving_positions` | `ticket` PK | Open/closed moving records with slot + prices |
| `constant_targets` | `(symbol, cycle_id, direction, slot_index)` | Precomputed targets + fired flag |
| `constant_queue` | autoincrement id | Currently-pending entries, deleted on success |
| `constant_tickets` | `ticket` PK | Open constant tickets, unbound to slots |

New methods: `save_moving_position`, `get_moving_positions(open_only)`, `mark_moving_position_closed`, `save_constant_targets` (bulk upsert), `get_constant_targets`, `mark_constant_target_fired`, `enqueue_constant_close`, `delete_constant_queue_entry(cycle, direction, slot)`, `bump_constant_queue_retry(cycle, direction, slot, count)`, `get_constant_queue`, `clear_constant_queue(cycle_id=None)`, `save/get/delete/clear_constant_tickets`, `clear_moving_positions`.

Save points (agent/02 §9 "after every mutation"): every moving open, every constant open, every moving closure, every queue enqueue/success/failure, every cycle end.

## 5. Reconciliation (`reconcile_on_startup`)

Per agent/02 §10 and the agent/05 atomicity rules:

1. Acquire `execution_lock`, set `catching_up = True` for the **entire pass** (finally-block resets it).
2. Load `symbol_state`; if not ACTIVE/RESETTING → clean slate, return.
3. `_rebuild_state_from_db`: restores phase, cycle, snapshot values, counts, both target lists (with fired flags), open moving records, constant tickets, pending queue (skipping fired targets).
4. `_replay_missed_closures`: diff DB-open moving tickets vs live MT5 tickets; for each missing ticket, log and call `_handle_moving_closure` — the identical live-tick path.
5. After replay: if either count reached its total, `_end_cycle(...)` runs — the same force-close-and-restart that would run live, **no special "recovered" branch**.
6. `catching_up = False`; live ticks resume.

Wired into `core/trading_engine.py`: `_reconnect_mt5()` calls `_reconcile_after_reconnect()` immediately after a successful `_init_mt5()`, before the tick loop resumes (per-symbol, error-isolated so one symbol's failure doesn't block others).

`_extract_close_info(ticket)`: `mt5.history_deals_get(position=ticket)`, filters `entry == DEAL_ENTRY_OUT` specifically, takes the **last** OUT deal, maps `reason == DEAL_REASON_TP` → "tp" (agent/05 multi-deal rule).

## 6. Config (`core/config_manager.py`)

- `get_default_symbol_config()` returns exactly the agent/02 §3 fields.
- `REMOVED_CONFIG_FIELDS` list — every Grid Bounce field (`tp_pips`, `sl_pips`, `second_entry_*`, lot arrays, `max_positions`, `sets`, `sets_config`, legacy center/pair lots) is **stripped on every load**, not just absent from defaults (agent/06 "not present-but-unused").
- `_validate_symbol_fields`: `constant_side` strict whitelist (warning + reset on violation — it logs, but never silently keeps a bad value); counts clamped 1..500; lots floored at 0.01; pips fields must be > 0; `constant_freq >= moving_freq` → **clamp to `moving_freq - 1.0` with a printed log line** (policy choice: clamp, logged — per Phase 2 prompt's "be consistent and log which one you chose").
- `update_config` merges only known new-schema fields; foreign keys are ignored, so stale UIs can't reintroduce removed fields.
- Old-format config files (from the Grid Bounce fork) auto-migrate: rebuilt from defaults, `max_runtime_minutes` carried over.
- `volatility_tolerance` removed from global config (that concept was Grid-Bounce-specific).

## 7. Orchestrator & API

- `core/strategy_orchestrator.py`: imports `QueuedCloseStrategyEngine as QCStrategy`; same start/stop/terminate surface; `get_status()` now aggregates `moving_total/constant_total/moving_closed/constant_closed/queue_length/catching_up` additively (per Phase 8 prompt — orchestrator unchanged structurally, engine interface matches what it already expected).
- `api/server.py`: `SymbolConfig` Pydantic model = the 8 new fields + `enabled`; `GlobalConfig` keeps `max_runtime_minutes` (and `volatility_tolerance` as accepted-but-unused so old clients don't 422). Route shapes unchanged. Supabase auth, per-user config files, DB-fresh-session behavior all untouched.
- `core/bot_manager.py`: zero changes needed — `get_or_create_bot` → orchestrator → `reconcile_strategies_on_startup()` already calls each engine's `reconcile_on_startup()`, which is exactly the new engine's entry point.

## 8. Activity logger

- `LEG_NAMES` reduced to exactly `MovingBuy`/`MovingSell`/`ConstantBuy`/`ConstantSell` (agent/04 naming rule — no Grid Bounce vocabulary).
- New `log_cycle_complete(cycle, reason, total_pnl, moving_closed, constant_closed)` — distinct from `log_reset` (which still exists but is now unused by the engine). Friendly strings for `NORMAL`, `EARLY_FORCE_CLOSE`, `TERMINATE_QUEUE_EXHAUSTED`.
- Every constant close logs which target (direction#slot, price) and which queue position released it; every enqueue logs the releasing moving ticket — "which moving position caused this constant to close" is answerable from the log alone (agent/04 logging rule).

## 9. UI (`static/index.html`)

- Per-symbol panel fields (agent/02 §13): `buy_count`, `sell_count`, `buy_lot`, `sell_lot`, constant-side `<select>` (Buy/Sell only, no blank/free-text, selected value comes from saved config so it can't flash a wrong default), `grid_distance`, `moving_freq`, `constant_freq`. All IDs use exact schema names (`buycount_`, `buylot_`, `constantside_`, `movingfreq_`, ...).
- Added a per-symbol **Kill** button (`/control/terminate/{symbol}`) — useful for testing terminate semantics.
- Status tiles: "Open Positions" (combined), **"Moving · Constant"** readout (`moving_closed/moving_total · constant_closed/constant_total`), "Close Queue" (queue length). Updated from `/status` every second in `fetchStatus()`.
- `buildConfigPayload()` sends only the new fields; old Grid-Bounce JS (sets, lot arrays, TP/SL pip inputs, second-entry overrides, volatility tolerance, volume-limit simulation) removed. Copy-Lots repurposed as a simple whole-config copy across symbols. Dead legacy helpers (`cloneSetData`, `fillSetInputs`, etc.) deleted; `updateLotInputs` kept as a no-op for old call sites.
