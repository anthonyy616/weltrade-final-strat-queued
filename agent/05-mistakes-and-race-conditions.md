# Queued Close Strategy — Mistakes & Race Conditions to Avoid

This document is not generic advice. Every item here is either a race condition that this codebase's specific async/MT5 architecture makes possible, or a mistake that has a concrete history in this project. Read it before writing the tick-handling and reconciliation code, not after something breaks.

## Why this matters more than usual here

This bot is tested by watching it live against a real MT5 terminal, not by unit tests catching a bug before it ships. That means a race condition doesn't show up as a red test — it shows up as "the bot closed the wrong position" or "a constant never fired" while Anthony is watching the Trade tab, hours into a test run, and now the whole session has to be redone. The cost of a race condition here is a full manual re-test, not a five-minute CI failure. Treat every section below as load-bearing.

## The event loop is single-threaded — but that doesn't mean single-operation

`trading_engine.py` distributes ticks to every symbol's orchestrator via `asyncio.gather`, and each symbol has its own `execution_lock`. This prevents two ticks *for the same symbol* from processing concurrently — but it does not prevent an `await` inside a locked block from yielding control back to the event loop, which can let a *different* code path (reconciliation, a config update handler, another symbol) run in the gap. The lock only protects against re-entrancy on the same symbol's tick handler if every mutation-relevant step is inside the lock's `async with` block, start to finish, with no early return that skips re-acquiring it later in the same logical operation.

**Concrete mistake to avoid:** splitting "detect closure" and "release queue entry" into two separate lock-acquiring blocks instead of one continuous one. If a second tick arrives between them, it can detect the same closure again (if the closed-flag hasn't been persisted yet) or process a queue release against state that's mid-mutation. Detection, queue release, and cycle-end check for a single tick must all happen inside one `async with self.execution_lock:` block, as one unit.

## The `catching_up` check must be atomic with the lock

Checking `if catching_up: return` before acquiring the lock, versus after, is not the same thing. If the check happens outside the lock, there's a window between the check and the lock acquisition where reconciliation could flip `catching_up` to `True` — the tick would then proceed to run live-tick logic concurrently with reconciliation's replay logic against the same state. The check needs to happen *inside* the same lock reconciliation itself uses, so the two are mutually exclusive by construction, not by hoping the timing works out.

## Reconciliation replay and live ticks must never interleave mid-replay

Reconciliation processes a list of missed closures one at a time, each one calling the same closure-handling function a live tick would call. If `catching_up` is only set for the duration of the *outer* reconciliation function but each individual replay step releases the lock in between (e.g., because each replay step is `await`ed separately with the lock re-acquired each time), a live tick could sneak in *between* two replay steps rather than before or after the whole batch. Hold `catching_up = True` for the entire reconciliation pass, and make sure the lock is held (or re-acquired without any gap where `catching_up` briefly reads `False`) across every replay step, not just around the outer function call.

## Iterating and mutating the same list

The close-queue is a list that gets read and mutated in the same pass (items removed on success, retry-count incremented on failure). Iterating `close_queue` directly while removing items from it during that same iteration will silently skip elements — a classic Python bug, not exotic. Always iterate over a snapshot (`for item in list(self.close_queue):`) and mutate the real list separately, or build a new list of survivors and reassign at the end. If a queue item goes missing without ever firing and without ever showing up in a retry-count, this is the first thing to check.

## SQLite writes across multiple symbols sharing one DB file

Each symbol gets its own `Repository` object, but in the current architecture all repositories for one user point at the same SQLite file. Concurrent writes from two symbols' tick handlers landing at nearly the same moment can produce a "database is locked" error even though each individual `Repository` awaits its own commits correctly — SQLite's file-level locking doesn't know or care that the writes came from logically independent symbols. If you see intermittent save failures under multi-symbol load, this is very likely why. Don't paper over it with a blind retry-forever on the save call; a save failure should be logged clearly and the in-memory state should remain authoritative until the next successful save, not silently dropped.

## `order_send` blocks the entire event loop, and startup here sends a lot of orders

`mt5.order_send` is a synchronous, blocking call. It is not wrapped in `run_in_executor` anywhere in the existing codebase, which means every order sent freezes tick processing for *every* symbol for the duration of that call. This is an existing, accepted characteristic of the codebase for the current strategies, which only ever send a handful of orders per event. This new strategy is different: startup can mean opening dozens or hundreds of positions in a tight sequential loop (buy_count + sell_count orders, each with its own `order_send` call and the existing `await asyncio.sleep(0.1)` position-confirmation pause). During that entire startup window, no other symbol's ticks are being processed, and ticks for *this* symbol that arrive during startup will queue up in the event loop rather than being dropped — meaning once startup finishes, the engine may process a small burst of ticks that are technically stale by a few hundred milliseconds. This is very unlikely to cause an actual strategy error, but it explains a specific symptom to expect during testing (a brief pause across all running symbols right when you start a heavily-populated symbol) rather than something to panic about or immediately mistake for a hang. If you see this and want it fixed, that's a `run_in_executor` change to raise explicitly — don't have the agent quietly restructure the order-execution pattern on its own initiative, since that's a change to a proven, working pattern in the base codebase.

## Config values must be snapshotted at cycle start, not read live every tick

The existing `GridBounceStrategyEngine` reads config values live via `@property` on every access, which is fine for that strategy because config changes are meant to take effect on the *next* opened position, not retroactively. This new strategy is different: `up_targets`, `down_targets`, and every moving position's TP/SL are all computed once, at cycle start, from `moving_freq`, `constant_freq`, and `grid_distance`. If the engine instead reads these three values live from config on every tick (instead of from the snapshotted `QueuedCloseState` fields), a user changing the closing frequency mid-cycle through the UI — which the existing auto-save UI pattern makes very easy to do accidentally — would silently corrupt the meaning of every already-precomputed target level, without any error or warning. The fix is structural, not defensive: never read `self.config.get('moving_freq', ...)` (or the other two) inside tick-handling or queue-release logic. Only read them once, at the top of `start()`, and store the values into the state object. Everything downstream reads from state, never from live config, for the lifetime of that cycle.

## MT5 ticket identity after `order_send`

The existing `_execute_market_order` pattern already handles this correctly (snapshot tickets before sending, re-query after, match by exclusion) — reuse that exact pattern for both moving and constant order opens in this new engine rather than trusting `result.order` directly as the final ticket number. On some account modes the ticket returned by `order_send` doesn't match the actual position ticket that appears in `positions_get()`. Getting this wrong here means the DB tracks a ticket that will never appear in a future `positions_get()` diff, which will make the very next reconnect reconciliation pass think that position closed while disconnected when it never actually opened correctly in the first place.

## Deal history can return more than one row per position

`mt5.history_deals_get(position=ticket)` can return an "in" deal (the open) and an "out" deal (the close) for the same position, and in netting-account modes can return partial deals too. Reconciliation must filter for the closing ("out") deal specifically — grabbing the first row in the result and assuming it's the close price is a mistake that will occasionally, silently, use the *open* price as if it were the close price, which then feeds a wrong number into `realized_pnl` and potentially into which constant-target gets released, since the direction/level lookup depends on knowing which side (TP or SL) actually fired.

## Terminate-all during an active queue-drain

If a user hits Terminate All while the close-queue still has pending entries, the terminate logic must clear `close_queue` and reset the counts as part of the same operation — not just close broker-side positions and leave the in-memory/DB queue state stale. A stale queue entry surviving a manual terminate is exactly the kind of state that will cause a confusing false start on the *next* cycle, and it will look like a brand-new bug rather than leftover state from the terminate action that caused it.

## Petty things that cause disproportionate re-test cycles

These are individually small, but each one is the kind of thing that looks fine in code review and only shows up once you're watching MT5 live — which is the expensive way to find out.

- A slot-index that's 0-based somewhere and 1-based somewhere else in the same codebase (config precompute vs. DB schema vs. logging) — pick 1-based everywhere per the architecture doc and never let a loop range silently introduce a mismatch.
- Logging a price with the wrong number of decimal places for the symbol's actual tick size, making it look like the bot computed the wrong number when it's actually a display-only rounding difference.
- The UI's constant-side dropdown defaulting to a value before config has loaded, then briefly flashing the wrong side before `loadConfig()` finishes — cosmetic, but will make you think the config didn't save correctly.
- Forgetting to clear per-cycle in-memory lists (`up_targets`, `down_targets`, `close_queue`) when a new cycle starts, relying instead on them "naturally" being small enough not to matter — over a long-running multi-cycle session this silently accumulates stale entries from prior cycles.
