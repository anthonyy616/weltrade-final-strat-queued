# Queued Close Strategy — Implementation Plan & Agent Prompts

This assumes the six planning docs (system comparison, architecture, implementation guide, coding style & rules, mistakes & race conditions, definition of done) are already sitting in the new repo, ideally under something like `docs/queued-close-strategy/`. Every prompt below tells the agent to read specific ones before starting — don't skip that step even if it feels redundant, since it's what keeps the agent's output consistent with everything we've already decided instead of drifting into its own naming or edge-case choices.

Work through the phases in order. Confirm each one against its own Definition-of-Done section, on a real MT5 demo connection where the phase says to, before moving to the next prompt. Don't queue up multiple phases at once even if you're confident — the whole point of this sequencing is catching a wrong assumption early, cheaply, before it's baked into three more phases built on top of it.

## Phase map and dependencies

1. Repo setup and doc placement
2. Config schema + `ConfigManager`
3. State model, DB schema, and pure formula functions (no trading yet)
4. Startup logic (opens real positions on a demo account)
5. Tick handling: closure detection, queue release, retry, cycle-end
6. Persistence wiring (save/load at every mutation point)
7. Reconciliation on reconnect
8. Orchestrator / BotManager / API route wiring
9. UI panel replacement
10. Guided manual soak test and fix loop

Phases 2–3 can be reviewed together since neither touches MT5. Phase 4 is the first one that actually opens broker positions — do not let the agent skip ahead to phase 5 logic inside the phase 4 prompt, even though it'll be tempting once positions are open.

---

## Phase 1 — Repo setup

```
Set up the new fork for the Queued Close Strategy project.

1. Copy the existing Weltrade fork as the starting point for this new repository — do not start from the Exness fork, since this strategy uses Weltrade's plain symbol handling with no account-type suffix.
2. Create a docs/queued-close-strategy/ folder and place the six planning documents I'm providing into it, named exactly as given: 01-system-comparison-and-buildout.md, 02-architecture.md, 03-implementation-guide.md, 04-coding-style-and-rules.md, 05-mistakes-and-race-conditions.md, 06-definition-of-done-checklist.md.
3. Do not modify any existing strategy logic yet. This phase is repo setup only.
4. Confirm the copied fork still runs as-is (server starts, existing UI loads, existing config file loads) before reporting this phase done — we're establishing a known-good baseline before changing anything.

Report back once you've confirmed the baseline runs, and list anything about the Weltrade fork's current structure that seems like it might conflict with what's described in 02-architecture.md, so we can resolve it before Phase 2 starts.
```

## Phase 2 — Config schema

```
Read docs/queued-close-strategy/02-architecture.md section 3 (config schema) and 04-coding-style-and-rules.md (naming section) before starting.

Implement the new per-symbol config schema exactly as specified in 02-architecture.md section 3: buy_count, sell_count, buy_lot, sell_lot, constant_side, grid_distance, moving_freq, constant_freq.

Requirements:
- constant_side must only accept "buy" or "sell" — reject anything else with a clear validation error, no silent fallback.
- constant_freq must be strictly less than moving_freq — reject or clamp on update, your choice, but be consistent and log which one you chose.
- Remove tp_pips, sl_pips, second_entry_*_pips, pair_buy_lots/pair_sell_lots/single_lots, max_positions, sets, and sets_config entirely from this fork's config — do not leave them present-but-unused.
- Update the Pydantic model(s) in api/server.py, the defaults and validation in ConfigManager, and get_default_symbol_config() to match.
- Do not touch anything related to trading logic yet — this phase is config schema and validation only.

Before reporting done, self-check against docs/queued-close-strategy/06-definition-of-done-checklist.md, the "Before claiming config schema is done" section, all five boxes. Actually restart the server and re-fetch /config to confirm persistence survives a restart — don't just check the in-memory object.

Report back with confirmation of each checklist box, and flag anything in the checklist you couldn't verify and why.
```

## Phase 3 — State model, DB schema, and pure formula functions

```
Read docs/queued-close-strategy/02-architecture.md sections 4, 6, and 9 before starting.

Create core/engine/queued_close_strategy_engine.py with:
- The dataclasses from architecture doc section 4 (MovingPositionRecord, ConstantTargetLevel, QueuedClose, QueuedCloseState) exactly as specified, including keeping up_targets and down_targets as two genuinely separate lists — do not merge them into one structure with a direction flag or sign convention.
- Pure functions implementing the closing-target formulas from section 6 — no MT5 calls, no async, just the math. These functions should take grid levels, frequencies, and slot counts as arguments and return the computed price lists.
- The new repository tables from section 9 (moving_positions, constant_targets, constant_queue, constant_tickets) added to the persistence layer's schema — table creation only, read/write methods can be stubbed for now, we're wiring those in Phase 6.

Do not open any MT5 positions in this phase. Do not implement tick handling yet. This phase is state shape and pure math only.

Verification: write out, in your response, the formula function's output for the exact worked example in architecture doc section 6 (center=1000, grid_distance=50, moving_freq=10, constant_freq=8, point=1, moving=BUY) and confirm it matches the numbers given there exactly. If it doesn't match, do not report this phase done — find and fix the discrepancy first.

Report back with the formula verification output and confirmation the new tables exist in the schema.
```

## Phase 4 — Startup logic

```
Read docs/queued-close-strategy/03-implementation-guide.md (startup pseudocode section) and 05-mistakes-and-race-conditions.md (the sections on MT5 ticket identity and config snapshotting) before starting.

Implement QueuedCloseStrategyEngine.start(): opens buy_count BUY positions and sell_count SELL positions at market, assigns TP and SL to every moving-side position using the formula functions from Phase 3, opens every constant-side position with no TP/SL fields sent at all, and precomputes up_targets/down_targets in full at startup.

Critical requirements, not optional:
- Read moving_freq, constant_freq, and grid_distance from config exactly once, at the top of start(), and store them into the state object. Every other part of this engine, in every future phase, must read these values from state — never from live config again after this point. This is the single most important rule in this phase; re-read the "config values must be snapshotted" section in 05-mistakes-and-race-conditions.md if this isn't clear.
- Use the same snapshot-tickets-before/re-query-after pattern the existing GridBounceStrategyEngine uses for order execution — do not trust result.order directly as the final ticket.
- Do not set sl or tp fields on any constant-side order request, under any circumstance.

This phase does open real positions on a connected MT5 account — test on a demo account, not live.

Before reporting done, run through every box in 06-definition-of-done-checklist.md's "Before claiming startup logic is done" section, using a real demo connection. For at least two moving positions, report the actual TP/SL values you observed in MT5's Trade tab, and confirm by hand that they match the formula's output for your test's configured inputs.

Report back with the observed MT5 values and confirmation of each checklist box.
```

## Phase 5 — Tick handling, queue, retry, cycle-end

```
Read docs/queued-close-strategy/03-implementation-guide.md (tick handling pseudocode and the edge-case policy table) and 05-mistakes-and-race-conditions.md in full before starting — this phase is where most of that document's warnings apply directly.

Implement on_tick() for QueuedCloseStrategyEngine:
1. Moving closure detection (reuse the existing position-drop-detection pattern from GridBounceStrategyEngine).
2. Queue release: for each closure, look up the corresponding ConstantTargetLevel by direction and slot index, enqueue it if not already fired.
3. Queue processing: attempt one close per pending item per tick. On failure, requeue and increment retry_count. If the failing item is the last one pending and it fails, terminate all positions for the symbol and start a new cycle — do not retry indefinitely.
4. Cycle-end check: if either side's closed count reaches its configured total, force-close everything remaining on the other side immediately, including anything in the queue, log the completion as NORMAL or EARLY_FORCE_CLOSE depending on whether both counts matched, and start a new cycle at current price.

Hard requirements from 05-mistakes-and-race-conditions.md:
- Detection, queue release, and cycle-end check for one tick must all happen inside a single continuous execution_lock block — no early exit that skips re-acquiring the lock for a later step in the same tick.
- Do not write any special-case "reversal" branch. The direction-keyed lookup (up_targets vs down_targets) should handle a reversal automatically. If you find yourself writing an if-reversed check, stop and reconsider the lookup logic instead.
- Iterate over a snapshot of the close queue when processing it (e.g. list(self.close_queue)), never the live list directly, since items are being removed during the same pass.
- Retry logic must attempt at most one close per tick per item — no internal loop that retries synchronously within a single tick's processing.

Before reporting done, work through every box in 06-definition-of-done-checklist.md's "tick handling / queue logic" and "retry / terminate-on-exhausted-queue" sections on a demo account — this includes manually forcing a closure, manually forcing the queued-behind scenario, forcing a reversal, and running an asymmetric count to completion.

Report back with what you observed for each of those manual tests, not just that the code compiles and runs.
```

## Phase 6 — Persistence wiring

```
Read docs/queued-close-strategy/02-architecture.md section 9 before starting.

Wire real save/load logic into the repository methods stubbed in Phase 3, and call them from every mutation point listed in architecture doc section 9: every moving closure, every queue release attempt (success or failure), and every cycle end.

On cycle end, explicitly delete that cycle's constant_queue rows rather than relying on the next cycle's writes to make old rows irrelevant.

Before reporting done, work through 06-definition-of-done-checklist.md's "persistence" section — this requires actually querying the SQLite file directly (sqlite3 CLI or a DB browser) after each type of mutation to confirm the row exists and is correct, not just checking that the save call didn't raise an exception.

Report back with the actual query output you used to verify each box.
```

## Phase 7 — Reconciliation on reconnect

```
Read docs/queued-close-strategy/02-architecture.md section 10, 03-implementation-guide.md's reconciliation pseudocode, and 05-mistakes-and-race-conditions.md's sections on catching_up atomicity and deal-history row filtering before starting.

Implement reconcile_on_startup() and wire it into trading_engine.py's reconnect path so it runs after a successful MT5 reconnect, before the tick loop resumes normal processing for that symbol.

Hard requirements:
- catching_up must be set True for the entire reconciliation pass, and on_tick's check for it must happen inside the same lock reconciliation uses, so the two can never run concurrently against the same symbol's state — not just "checked early in the function."
- Every replayed closure must call the exact same closure-handling logic on_tick uses (from Phase 5) — do not write a separate implementation of queue-release logic for the reconciliation path.
- When filtering mt5.history_deals_get() results, use the closing ("out") deal specifically, not the first row returned — a position can have more than one deal record.
- If reconciliation determines the cycle had already ended during the outage, the same force-close-and-restart logic from Phase 5 should run — no separate "recovered" code path.

This phase requires testing an actual hard kill of the bot process (not a graceful stop) while positions are open and mid-closure, followed by a real restart.

Before reporting done, work through every box in 06-definition-of-done-checklist.md's "reconciliation / crash recovery" section. This is the highest-risk phase in the whole project — take the extra time here rather than rushing to report it done.

Report back with a description of exactly what you killed, when, and what the bot correctly recovered versus anything it got wrong on the first attempt (and what you changed to fix it).
```

## Phase 8 — Orchestrator / BotManager / API wiring

```
Confirm that BotManager, StrategyOrchestrator, and the FastAPI routes in api/server.py work with QueuedCloseStrategyEngine the same way they work with the existing engine — same per-user isolation, same start/stop/terminate-all/per-symbol control surface, same Supabase auth.

You should not need significant new code here if the engine's public interface (start, stop, terminate, on_external_tick, get_status) matches what the orchestrator already expects — if it doesn't match, adjust the engine's interface to fit the existing orchestrator pattern rather than modifying the orchestrator itself, unless you hit a case where the orchestrator genuinely can't express something this strategy needs (for example, if get_status needs new fields like moving_closed_count and constant_closed_count for the UI in Phase 9 — that's fine to add, additively).

Confirm multi-symbol operation works: run at least two symbols with this strategy enabled at once and confirm each maintains independent state and independent queues with no cross-symbol interference.

Report back once basic start/stop/terminate-all work end to end through the API for at least two symbols simultaneously.
```

## Phase 9 — UI panel

```
Read docs/queued-close-strategy/02-architecture.md section 13 before starting.

Replace the per-symbol settings panel in static/index.html: remove the TP/SL pip inputs, lot arrays, max-positions, and sets/sets_config UI entirely for this fork, and add buy_count, sell_count, buy_lot, sell_lot, a constant-side <select> (options: Buy, Sell only, no free text, no blank option), moving_freq, constant_freq, and keep grid_distance.

Add a status readout showing moving closed/total and constant closed/total separately, distinct from the existing combined position-count tile.

The constant-side dropdown's default, on page load, must reflect what's actually saved in config — don't let it flash a hardcoded default before loadConfig() finishes.

Before reporting done, work through 06-definition-of-done-checklist.md's "UI" section: save and reload after an actual page refresh (not just in-session), and confirm the status readout's numbers match what you can count directly in MT5 during a live test.

Report back with confirmation of each checklist box.
```

## Phase 10 — Guided soak test and fix loop

This phase isn't a single agent prompt — it's Anthony running a real multi-hour (or multi-day) session against a demo account, using 06-definition-of-done-checklist.md as the running reference, and reporting specific failures back. When something breaks, use this shape rather than a vague "it's not working" report, since a precise report here is the difference between a five-minute fix and the agent guessing:

```
Bug report for [symbol / phase]:

What I configured: [buy_count, sell_count, lots, constant_side, grid_distance, moving_freq, constant_freq]
What I expected: [reference the specific formula or policy from the docs]
What actually happened: [exact prices/counts observed in MT5, with timestamps if possible]
What the bot's logs said at that moment: [paste the relevant log lines]

Check docs/queued-close-strategy/05-mistakes-and-race-conditions.md first — if this matches one of the documented failure modes, say which one you think it is and why, then fix it. If it's a new failure mode not covered there, fix it and then add it to that document so it's caught by the checklist next time.
```
