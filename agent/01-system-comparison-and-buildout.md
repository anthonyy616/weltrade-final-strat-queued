# Grid Bounce vs. Queued Close Strategy — Comparison & Build-Out Guide

This document exists so that anyone (human or AI agent) picking up the Queued Close Strategy fork understands exactly what it's built from, what it keeps, and what it replaces. Read this before touching any code.

## Part 1 — The current system (Grid Bounce)

### What it is

Grid Bounce is a Python trading bot for MetaTrader 5 that trades synthetic indices (FX Vol / SFX Vol on Weltrade; forex/gold/indices/crypto on Exness). It runs as a FastAPI server with a Supabase-authenticated web UI, one bot instance per logged-in user, one strategy engine per enabled symbol.

### Strategy logic (what actually trades)

1. **Startup**: opens exactly one BUY and one SELL at the current market price (the "center"). This pair is `grid_level_1`.
2. **First move**: when price moves `grid_distance` pips away from center (up or down), the position on the *wrong* side at center is closed (FIFO), and three new positions open at the new level (`grid_level_2`): a new BUY, a new SELL, and one directional "single" (extra BUY if price went up, extra SELL if price went down).
3. **Bouncing**: from then on, the bot alternates between `grid_level_1` and `grid_level_2` — each time price crosses from one level to the other, it closes the trailing position at the level being left and opens three new ones at the level being entered. This is the actual "bounce."
4. **Position limits**: `max_positions` (a multiple of 3) caps how many groups of three can open. In the Exness fork this expanded into "sets" — multiple independently-configured position groups that activate in sequence once the prior set hits its cap.
5. **Exit**: every individual position has its own broker-side TP/SL, set at open time (in the Exness fork, aligned across a level via a reference-anchor system so pair positions on the same level share consistent exit prices). Any single position hitting its TP or SL triggers a **nuclear reset**: every open position for that symbol is force-closed, state resets, and a brand-new cycle starts immediately at the current price.
6. **Stop conditions**: a `max_runtime_minutes` timer triggers a graceful stop (let the current cycle finish, then stop) with a 5-minute hard-stop failsafe. "Terminate All" closes everything immediately without waiting.

### Architecture (the plumbing, not the strategy)

- `core/trading_engine.py` — owns the MT5 connection, health-checks it, reconnects on failure, runs the tick loop, distributes ticks to every active orchestrator.
- `core/bot_manager.py` — one `StrategyOrchestrator` per authenticated user, created lazily, restored on restart.
- `core/strategy_orchestrator.py` — per-user, holds one strategy engine per enabled symbol, routes ticks, exposes start/stop/terminate at the all-symbols or per-symbol level, owns the `SessionLogger`.
- `core/config_manager.py` — per-user JSON config file, one entry per symbol, with defaults, validation, and clamping on every update.
- `core/engine/grid_bounce_strategy_engine.py` — the actual strategy state machine described above.
- `core/engine/activity_logger.py` — per-symbol, human-readable append-only log file, one line per trading event.
- `core/session_logger.py` — per-user session log, aggregates activity across symbols for one login session.
- `core/persistence/repository.py` — SQLite (via `aiosqlite`) per-process DB, one row per symbol tracking phase/prices/cycle/metadata, used for crash recovery.
- `api/server.py` — FastAPI routes: config CRUD, start/stop/terminate (all and per-symbol), status polling, history/log retrieval, Supabase auth middleware.
- `static/index.html` — single-page UI: login, per-symbol config panel (checkboxes to enable symbols, then a settings accordion per enabled symbol), global settings, start/stop/terminate buttons, live terminal, session history.

### Fork differences (Exness vs. Weltrade)

Weltrade requires a desktop MT5 terminal installation with no suffix logic — the UI symbol name (`FX Vol 20`) is the literal MT5 symbol name. Exness requires an account-type-dependent suffix (`m`, `z`, or none) appended to the base symbol, resolved via `mt5_symbol` at call time, plus a volume-splitting mechanism (`EXNESS_MAX_LOT`) for lot sizes above the broker cap, plus multi-set support the Weltrade fork doesn't have.

## Part 2 — The new system (Queued Close Strategy)

### What it is

A completely different strategy that happens to share the same broker plumbing. It opens a large, user-configured pool of positions all at once, and instead of ever opening more, it spends the rest of the cycle closing them down in a specific, queued order. There is no bouncing, no nuclear reset on a single TP/SL, and no per-symbol position limit to grow into — the pool size *is* the limit, set once at startup.

### Strategy logic

**Startup**: the user configures, per symbol: a BUY count and lot size, a SELL count and lot size, which side is "moving" and which is "constant," a moving closing frequency (pips), a constant closing frequency (pips), and a grid distance (pips). At bot start, *all* buy positions and *all* sell positions open immediately at market, at their configured lot sizes. The constant side gets no broker-side TP/SL at all — ever. The moving side gets both a TP and an SL set on every position, computed from the grid distance and moving frequency (see formulas below).

**Grid distance** no longer triggers new position opens. It only marks two price levels — `center + grid_distance` and `center - grid_distance` — from which the closing schedule is measured outward in both directions.

**Closing math.** Let `diff = moving_freq - constant_freq` (the constant frequency is always assumed smaller than the moving frequency). For `n` = 1 up to the moving count:

- If moving = BUY:
  - Up-direction TP levels: `grid_level_up + n * moving_freq`
  - Down-direction SL levels: `grid_level_down - n * moving_freq`
  - Constant (SELL) target paired with an up-direction closure: `that level - diff`
  - Constant (SELL) target paired with a down-direction closure: `that level - diff`
- If moving = SELL:
  - Down-direction TP levels: `grid_level_down - n * moving_freq`
  - Up-direction SL levels: `grid_level_up + n * moving_freq`
  - Constant (BUY) target paired with either direction's closure: `that level + diff`

In other words: constant targets always sit `diff` pips *behind* the moving level that just fired — on the side that's already been reached — never ahead of it. This is what makes "constant closes only after the paired moving position has closed" true by construction rather than by a runtime check.

**The queue.** Every moving closure (broker TP or SL fire) releases exactly one constant close, at the precomputed target for that closure's direction and index. If price already passed that constant's target level before the moving closure happened, the constant close is queued rather than fired early, and it fires together with (in the same processing pass as) the moving closure that unlocks it. Multiple moving closures detected in a single tick release their constants in slot order, all in one pass. A constant close that fails to send to the broker is requeued, not dropped — if it's the last item in the queue and it still fails, the bot terminates all positions for that symbol and restarts the cycle.

**Reversals.** Both the up-direction and down-direction constant target lists are precomputed in full at startup (constant count × 2 levels total). The bot tracks a running count of constants closed against the configured total, not a binding of specific constant tickets to specific slots — so if price reverses mid-cycle, the bot simply starts releasing from the other direction's precomputed list, with no recalculation needed.

**Asymmetric counts and cycle end.** If buy count and sell count differ, the cycle ends the instant *either* side (moving or constant) reaches its configured closed-count. At that moment, every remaining open position on the *other* side is force-closed at market immediately — including anything still sitting in the queue. This is the one and only cycle-end condition; there is no separate "nuclear reset on any TP/SL" concept here. An early finish (one side ran out before the other) is logged distinctly from a normal simultaneous finish.

**New cycle**: starts immediately at the current market price, same configuration, same as Grid Bounce's restart-after-reset behavior.

**Crash and reconnect.** On reconnect, before any live tick is processed: pull every open MT5 position for the symbol (by magic number), diff it against what the DB says should be open. For every ticket that's missing, pull its MT5 deal history to get the exact close price and whether it hit TP or SL, then replay that closure through the *same* closure-handling function a live tick would use — same queue release, same retry-on-failure logic. Only once that catch-up pass is fully drained does the bot resume processing live ticks. If the catch-up reveals the cycle had already ended while disconnected, the force-close-and-restart runs exactly as it would live.

### Side-by-side summary

| Aspect | Grid Bounce | Queued Close Strategy |
|---|---|---|
| Positions at startup | 1 buy + 1 sell | N buys + M sells (user-set, can differ) |
| New positions after startup | Yes, on every grid-distance crossing | Never — pool is fixed at startup |
| Grid distance meaning | Triggers new opens, bot bounces between two levels | Marks the two closing-start levels only |
| TP/SL | Every position has one, aligned per-level | Only moving positions have TP/SL; constant positions have none |
| Exit trigger | Any TP/SL hit → nuclear reset of everything | Cycle ends only when one side's full count is closed |
| Position count over a cycle | Grows via sets/groups, capped by max_positions | Fixed and shrinking from the start |
| Recovery model | Reconcile phase/prices from DB | Reconcile which specific tickets closed via deal history, replay closures |

## Part 3 — Building on top of the existing codebase

### Reuse as-is (broker/tick plumbing, not strategy logic)

- `trading_engine.py`'s connection management, health checks, and reconnect loop — add the reconciliation-before-resuming-ticks step described above, but the connection-retry mechanism itself does not change.
- `bot_manager.py` and `strategy_orchestrator.py` patterns — one orchestrator per user, one engine per symbol, same start/stop/terminate surface.
- `session_logger.py` as-is.
- `activity_logger.py`'s file-per-symbol, timestamped, human-readable pattern — but its `LEG_NAMES` dictionary and event-writing methods need new entries (see the architecture doc).
- `repository.py`'s SQLite-via-aiosqlite pattern and per-symbol row/table shape — extended with new tables, not replaced.
- Supabase auth, per-user config file naming (`config_<user_id>.json`), FastAPI route shape in `api/server.py`.
- The overall `static/index.html` page structure — login flow, status tiles, live terminal, session history panel.

### Replace or build new

- The strategy engine itself: a new class, `QueuedCloseStrategyEngine`, in a new file `core/engine/queued_close_strategy_engine.py`. Nothing from `GridBounceStrategyEngine`'s bounce/reset logic carries over; only the *pattern* of how it talks to MT5 (order execution, position closing, tick handling entry point) is a useful reference.
- `ConfigManager`'s per-symbol schema — new fields, no `tp_pips`/`sl_pips`/lot-arrays/sets concepts.
- New repository tables for moving positions, precomputed constant target lists, and the close-queue (full shape in the architecture doc).
- The per-symbol settings panel in the UI — smaller than the current one, with entirely different fields.
- `ActivityLogger`'s `LEG_NAMES` and logging methods for the new leg types and the early-completion vs. normal-completion distinction.

### Suggested build order

1. Config schema + `ConfigManager` validation for the new fields (fast to get right, unblocks everything else).
2. `QueuedCloseStrategyEngine` startup logic (open the full pool, compute and send TP/SL for the moving side, precompute both constant target lists) — test this alone first by watching MT5 directly before writing any closing logic.
3. Tick handling: moving-closure detection → constant queue release → order-level retry → terminate-all-on-exhausted-retry.
4. Reversal handling (should require zero extra code if the running-count model is implemented correctly — this is a good sign to test for, not a separate feature to build).
5. Asymmetric-count force-close and cycle-end logging distinction.
6. Persistence: new repository tables, save/load on every state change.
7. Reconciliation-on-reconnect, wired into `trading_engine.py`'s reconnect path.
8. UI panel replacement, new status tiles for moving/constant split if desired.
9. End-to-end soak test on a demo account, watching MT5's position and history tabs directly against the bot's own logs, deliberately triggering a mid-cycle disconnect to verify reconciliation.
