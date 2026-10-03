# Queued Close Strategy — Coding Style & Rules

These rules exist because of specific, previously-hit bugs in this codebase's history, plus the working relationship between Anthony and the coding agent. Follow them as hard constraints, not suggestions.

## State design

- **No shadow state.** Every piece of state lives in exactly one place. The existing codebase has already hit a real bug from this: `StrategyState` once carried duplicate top-level fields mirroring set-level fields, with an `if set_index == 0:` block trying to keep them in sync — this created silent drift. Do not repeat this pattern here. If a value is derivable (e.g. `moving_total` from config), derive it via a property rather than storing and syncing a duplicate copy.
- **Two lists stay two lists.** `up_targets` and `down_targets` must never be merged into one array with a sign convention, a shared offset, or a "direction flag on one shared object" — keep them as genuinely separate collections. This was a deliberate design decision to eliminate an entire class of overwrite bug; don't optimize it away.
- **Running counts, not slot binding.** `moving_closed_count` / `constant_closed_count` against configured totals is the correct model — do not introduce a parallel "which specific constant ticket maps to which slot" tracking structure. The strategy was explicitly designed so that any open constant ticket can satisfy any released queue entry; adding slot-binding on top of this adds complexity that solves nothing and risks the two bookkeeping mechanisms disagreeing.

## Naming

- New engine class: `QueuedCloseStrategyEngine`, file `core/engine/queued_close_strategy_engine.py`.
- Leg names for logging: `MovingBuy`, `MovingSell`, `ConstantBuy`, `ConstantSell`. Never reuse `PairBuy`, `PairSell`, `SingleBuy`, `SingleSell`, `CenterBuy`, `CenterSell`, or anything from the Grid Bounce vocabulary — those names carry specific meaning in that strategy that doesn't apply here, and reusing them will confuse anyone reading logs later.
- Config field names must match the architecture doc exactly (`buy_count`, `sell_count`, `buy_lot`, `sell_lot`, `constant_side`, `grid_distance`, `moving_freq`, `constant_freq`). Do not introduce synonyms (`buys`, `n_buy`, `constant_lot_size`, etc.) partway through implementation — pick the names once and use them everywhere, including in the DB schema and the UI's HTML element IDs.

## MT5 interaction

- Always resolve `mt5_symbol` through the property, never use `self.symbol` directly in a broker call, even though this fork has no suffix logic today. If a suffix requirement is ever added later, every direct `self.symbol` usage becomes a silent bug at that point — going through the property costs nothing now and prevents that entire failure class.
- Never set `sl`/`tp` fields on a constant-side order request. This is worth a comment at the call site, not just a mental rule — a future edit is more likely to accidentally "helpfully" add default TP/SL handling if there's nothing marking that spot as deliberate.
- Reuse the existing `point`-per-symbol-type logic (JPY pairs, gold, indices, crypto, standard forex all differ) rather than hardcoding a pip-to-price conversion anywhere in the new engine.
- Position-close and order-send failures are surfaced by return value, not exceptions, matching the existing codebase's convention — don't introduce a different error-handling style for this one engine.

## Concurrency

- All state mutation happens inside the same `asyncio.Lock` pattern the existing engine uses (`execution_lock`) — tick processing, queue release, and reconciliation replay must never run concurrently against the same symbol's state.
- The `catching_up` flag check must be the very first thing `on_tick` does, before touching any other state, full stop.
- Retry-on-failed-close must not loop synchronously within a single tick call — one attempt per tick, tracked via `retry_count`, so a string of failures doesn't block the event loop or delay other symbols.

## Persistence

- Save state after every mutation that would be expensive or ambiguous to reconstruct from MT5 alone on a crash: a moving closure, a queue release attempt (success or failure), a cycle-end. Don't batch these into a periodic save — the reconciliation logic depends on the DB being close to real-time accurate.
- When a cycle ends (either normally or via early force-close), explicitly clear that cycle's `constant_queue` rows rather than assuming the next cycle's fresh writes will make old rows irrelevant — a stale row with a mismatched `cycle_id` sitting in the table is a latent bug waiting for the next reconciliation pass to trip over it.

## Logging

- Cycle completion gets its own explicit log call and reason field (`NORMAL` vs `EARLY_FORCE_CLOSE`) — do not fold this into the existing `log_reset` method, which is conceptually a different event (a single TP/SL-triggered nuclear reset) that doesn't exist in this strategy at all.
- Every constant close should log which moving closure released it (ticket, slot, direction) — when debugging a live issue, "which moving position caused this constant to close" is the first question that gets asked, and it should be answerable from the log file alone without cross-referencing the DB.

## General working rules (carried over from how this project already runs)

- **Surgical changes, not rewrites**, even within this new engine once it exists — once a function is working and tested, a later change to it should be the minimum diff that achieves the new requirement, not a rewrite of the whole function.
- **No judgment calls left implicit.** Every edge case in the policy table in the implementation guide is a hard requirement, not a default to override if it seems inconvenient during implementation. If a genuinely new edge case comes up that isn't covered by that table, stop and ask rather than guessing at what seems reasonable.
- **Test against MT5 directly, not just the bot's own logs**, at every milestone in the implementation guide's build order — the bot's internal state can be self-consistent and still wrong relative to what's actually happened on the broker, and the whole point of the testing checklist is to catch that category of bug before it reaches production.
