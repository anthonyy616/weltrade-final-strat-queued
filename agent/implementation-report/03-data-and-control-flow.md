# Data & Control Flow Reference

Diagrams of how data moves through the system as implemented. Use this while watching MT5 during testing — each arrow corresponds to a log line.

## Cycle lifecycle

```
start()                          [user: Start Bot / Start {symbol}]
  │ validate constant_side
  │ snapshot config → state      (ONLY config read in the engine's life)
  │ cycle_count += 1, clear cycle state
  ▼
_start_new_cycle_at_market()
  │ tick → center, grid_level_up/down = center ± grid_distance
  │ compute_constant_targets() → up_targets, down_targets (precomputed in full)
  │ persist constant_targets rows
  │ open moving × moving_total  (each with slot-n TP/SL)  → save_moving_position
  │ open constant × constant_total (NO sl/tp keys)        → save_constant_ticket
  │ phase = ACTIVE, save symbol_state
  ▼
ACTIVE ── ticks ──▶ closures & queue (below) ──▶ _check_cycle_end
  │                                                │ either side reaches total
  │                                                ▼
  │                                            _end_cycle(reason)
  │                                              force-close remainder
  │                                              clear queue/tickets/moving DB rows
  │                                              log_cycle_complete(NORMAL | EARLY_FORCE_CLOSE)
  │                                              graceful_stop? → IDLE : new cycle at market
  ▼
IDLE (graceful stop complete / terminate)
```

## Per-tick flow (phase = ACTIVE)

```
trading_engine tick loop
  └─ orchestrator.on_external_tick(symbol, tick_data)
      └─ engine.on_external_tick(ask, bid)
          └─ async with execution_lock:            ◄── ONE block, whole tick
              ├─ catching_up? → return             (check inside the lock)
              ├─ phase != ACTIVE? → return
              │
              ├─ _process_closures_and_queue(ask, bid)
              │    ├─ live = positions_get(symbol)
              │    ├─ closed = tracked_open − live   (sorted by slot_index)
              │    └─ for each closed ticket:
              │         _handle_moving_closure(ticket)     ◄── single closure path
              │              ├─ rec.closed = True, moving_closed_count += 1
              │              ├─ TP/SL from deal history (fallback: proximity)
              │              ├─ PnL += …, log_tp_hit/log_sl_hit
              │              ├─ mark_moving_position_closed(ticket)
              │              └─ _enqueue_constant_target(direction, slot, released_by=ticket)
              │                   ├─ already fired? already queued? → skip
              │                   ├─ close_queue.append(QueuedClose)
              │                   └─ enqueue_constant_close DB row
              │    └─ _process_close_queue()
              │         └─ for item in list(close_queue):        (snapshot!)
              │              ticket = constant_tickets[0]        (FIFO, no slot binding)
              │              _close_position(ticket)             (ONE attempt)
              │              ├─ success: fired=True, constant_closed_count += 1,
              │              │     pop ticket, remove item,
              │              │     delete ticket row + queue row, mark target fired
              │              └─ failure: retry_count += 1, bump DB
              │                    └─ last item pending? → _end_cycle(TERMINATE_QUEUE_EXHAUSTED)
              │                       else: leave for next tick
              │
              ├─ _check_cycle_end(ask, bid)
              │    moving_closed ≥ moving_total  OR  constant_closed ≥ constant_total
              │    → _end_cycle(EARLY_FORCE_CLOSE if counts unequal else NORMAL)
              │
              └─ graceful_stop and phase == IDLE → running = False
```

## Direction & reversal model

Every moving closure releases exactly one constant target:

| Moving side | Closure | Direction released | Constant target price |
|---|---|---|---|
| BUY | TP | `up` slot n | `up_TP_level_n − diff` |
| BUY | SL | `down` slot n | `down_SL_level_n − diff` |
| SELL | TP | `down` slot n | `down_TP_level_n + diff` |
| SELL | SL | `up` slot n | `up_SL_level_n + diff` |

where `diff = moving_freq − constant_freq` (> 0, enforced by config validation).

Reversal requires zero code: if price reverses mid-cycle, the next closure comes from whichever direction's slot fires next, and `_handle_moving_closure` looks up `up_targets` or `down_targets` purely from that closure's own direction. Nothing tracks "which direction we're in."

## Persistence map

| Event | DB writes |
|---|---|
| Cycle start | `constant_targets` bulk insert; `symbol_state` upsert |
| Each moving open | `moving_positions` insert |
| Each constant open | `constant_tickets` insert |
| Moving closure | `moving_positions.closed = 1` |
| Queue enqueue | `constant_queue` insert |
| Queue success | `constant_tickets` delete + `constant_queue` delete + `constant_targets.fired = 1` |
| Queue failure | `constant_queue.retry_count` bump |
| Cycle end | force-close; `constant_queue` + `constant_tickets` + `moving_positions` cleared for the cycle; `symbol_state` upsert |
| Terminate | same clears as cycle end, plus in-memory reset, `symbol_state` IDLE |

## Reconciliation flow

```
MT5 connection lost
  └─ trading_engine._reconnect_mt5()
       ├─ _init_mt5() retry loop (unchanged)
       └─ on success: _reconcile_after_reconnect()
            └─ per running strategy: engine.reconcile_on_startup()
                 └─ async with execution_lock:
                      catching_up = True          (whole pass)
                      ├─ load symbol_state → ACTIVE/RESETTING?
                      │    no → clean slate, done
                      ├─ _rebuild_state_from_db
                      │    (phase, cycle, snapshot, counts, targets+fired,
                      │     open moving records, constant tickets, pending queue)
                      ├─ _replay_missed_closures
                      │    missing = DB_open_tickets − live_tickets
                      │    for each: _handle_moving_closure(ticket)   ◄── same path as live
                      └─ either count ≥ total → _end_cycle(...)       (no special branch)
                      catching_up = False
```

Note the engine-level `execution_lock` is the same lock `on_external_tick` uses — a live tick arriving mid-reconciliation blocks on the lock and then sees `catching_up = False` only after the pass completes. There is no window where both run against the same state.

## Status surface (`/status`)

```
engine.get_status() → {
  running, phase, cycle_count, center_price,
  open_positions,                     # combined (backward compat)
  moving_total, constant_total,
  moving_closed, constant_closed,
  moving_open, constant_open,
  queue_length, catching_up,
  realized_pnl, graceful_stop, is_resetting,
  step, iteration, current_price
}
orchestrator.get_status() → aggregates the above across symbols + per-symbol "strategies" dict
UI fetchStatus() → Open Positions tile, Moving · Constant readout, Close Queue tile
```
