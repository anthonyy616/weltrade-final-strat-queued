# API & Config Reference

## Per-symbol config schema

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

| Field | Type | Validation | Notes |
|---|---|---|---|
| `enabled` | bool | — | Toggles the symbol's engine in the orchestrator |
| `buy_count` | int 1–500 | clamped, logged | Total BUY positions opened at cycle start |
| `sell_count` | int 1–500 | clamped, logged | Total SELL positions opened at cycle start |
| `buy_lot` | float ≥ 0.01 | floored | Lot per BUY position |
| `sell_lot` | float ≥ 0.01 | floored | Lot per SELL position |
| `constant_side` | `"buy"` \| `"sell"` | **strict** — invalid value reset to `"sell"` with logged warning | Moving side is the other one; never stored separately |
| `grid_distance` | float > 0 | reset to default if ≤ 0 | Raw price units (see D1 in `02-design-decisions.md`) |
| `moving_freq` | float > 0 | reset to default if ≤ 0 | Closing frequency of the moving side |
| `constant_freq` | float > 0 | **clamped to `moving_freq − 1.0`** if ≥ `moving_freq` (logged) | Must be strictly less than moving_freq — baked into the target math |

Global config: `{ "max_runtime_minutes": 0 }` (0 = no timeout). `volatility_tolerance` is accepted by the API model but unused (J7 in `02-design-decisions.md`).

**Distances are raw price units**, matching the previous fork's convention — e.g. `grid_distance: 25` on FX Vol 20 means 25 price units, same as the old working config.

## Removed fields (stripped on load, rejected on update)

`tp_pips`, `sl_pips`, `second_entry_buy_tp_pips`, `second_entry_buy_sl_pips`, `second_entry_sell_tp_pips`, `second_entry_sell_sl_pips`, `pair_buy_lot`, `pair_sell_lot`, `single_lot`, `center_buy_lot`, `center_sell_lot`, `pair_buy_lots`, `pair_sell_lots`, `single_lots`, `max_positions`, `sets`, `sets_config`.

`update_config` only merges recognized field names — a stale client sending `sets` or `tp_pips` has them ignored, not persisted.

## API endpoints (unchanged shapes)

| Endpoint | Method | Notes |
|---|---|---|
| `/config` | GET | Full config; old-format files migrated on load |
| `/config` | POST | Partial update (global + per-symbol); validated/clamped server-side |
| `/control/start` | POST | ⚠️ Deletes `db/grid_v3.db` first — fresh session, wipes recovery state |
| `/control/stop` | POST | Graceful stop all |
| `/control/start/{symbol}` | POST | Enables symbol if needed, starts it |
| `/control/stop/{symbol}` | POST | Graceful stop one |
| `/control/terminate/{symbol}` | POST | Close everything for symbol, full reset |
| `/control/terminate-all` | POST | Close all + delete DB |
| `/status` | GET | See status shape in `03-data-and-control-flow.md` |
| `/history`, `/history/groups`, `/history/activity` | GET | Unchanged |

## `/status` new fields

```json
{
  "moving_total": 5, "constant_total": 5,
  "moving_closed": 3, "constant_closed": 2,
  "queue_length": 1, "catching_up": false
}
```

Aggregated across symbols at the orchestrator level; per-symbol values in `strategies.{symbol}`.

## Database tables

SQLite at `db/grid_v3.db` (deleted on boot / Start All — see D2).

```sql
moving_positions   (ticket PK, symbol, cycle_id, entry, tp_price, sl_price,
                    direction, slot_index, closed)
constant_targets   (symbol, cycle_id, direction, slot_index,   -- composite PK
                    price, fired)
constant_queue     (id AUTOINCREMENT PK, symbol, cycle_id, direction,
                    slot_index, retry_count, enqueued_at)
constant_tickets   (ticket PK, symbol, cycle_id)
```

Legacy tables (`symbol_state`, `grid_pairs`, `ticket_map`, `trade_history`) still exist; this engine uses `symbol_state` (phase/center/cycle_id + JSON metadata with the config snapshot and running counts) and ignores `grid_pairs`/`ticket_map`.

Query examples for testing:

```bash
sqlite3 db/grid_v3.db "SELECT ticket, slot_index, closed FROM moving_positions WHERE symbol='FX Vol 20';"
sqlite3 db/grid_v3.db "SELECT direction, slot_index, price, fired FROM constant_targets WHERE symbol='FX Vol 20' AND cycle_id=1 ORDER BY direction, slot_index;"
sqlite3 db/grid_v3.db "SELECT * FROM constant_queue;"          -- must be empty between cycles
sqlite3 db/grid_v3.db "SELECT cycle_id, phase, metadata FROM symbol_state WHERE symbol='FX Vol 20';"
```

## Log files

- `logs/users/{user_id}/sessions/activity_{symbol}_{date}.log` — human-readable, per-symbol. `log_cycle_complete` line: `Cycle #n COMPLETE | Reason: … | Moving closed: x | Constant closed: y | Cycle P&L: $z`.
- Every queue event names its releasing moving ticket and the target (`up#3 @ 1058`), so "which moving position caused this constant to close" is answerable from the log alone.
- `logs/bot.log` and `logs/terminal_output.log` — engine/system level, including `[RECONCILE] {symbol}: {summary}` lines after reconnects.
