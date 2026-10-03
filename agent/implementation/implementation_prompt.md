# 09 - Limit-Trigger Open Mode: Agent Prompts

How to use: paste the Ground Rules prompt once at the start of the session, then paste one phase prompt at a time. Do not let the agent start the next phase until you have reviewed its self-certification. Doc 08 is the source of truth. If a prompt and doc 08 disagree, the agent must stop and ask.

---

## Ground Rules (paste first)

```
You are extending the queued-close fork with a new, optional "limit_trigger" open mode.

Before anything else, read in full: docs 01-07 in the repo (especially 05 mistakes/race conditions and 06 definition of done), then 08-limit-trigger-implementation-plan.md. Doc 08 is the source of truth for this work.

Ground rules for the whole task:
1. Existing burst mode must keep working exactly as before and stays the default. Any change that could alter burst behavior must be called out explicitly before you make it.
2. Make surgical changes. No unrelated refactors, renames, or reformatting. Follow the existing code style and the conventions in doc 04.
3. The EA must never block. No waiting loops inside OnTradeTransaction, OnTimer or the command poll. All waiting is state plus deadlines driven by events and the timer. The EA must keep servicing abort commands while armed.
4. Every wait on the Python side has a deadline. Nothing can hang.
5. State lives in one place only. Do not mirror fields across levels or files.
6. Use the suffixed MT5 symbol name consistently for every tick, order, positions_get and orders_get call.
7. Python never trusts the EA's result alone. Always reconcile against positions_get and orders_get.
8. Every failure path must end in: remove pendings, flatten any positions, restart the cycle or stop via the circuit breaker. Never leave a pending order behind.
9. Do not assume file formats, field names or helper names from doc 08. Read the real code (WTExecutor.mq5, ea_bridge.py, bulk_orders.py, the engine, repository.py, config_manager.py, api/server.py, static/index.html) and adapt. Field names in doc 08 are logical.
10. I test by watching MT5 live, not with automated tests, so every race condition you miss costs me a full manual cycle. Think through races before writing code, and list the ones you considered in your report.

After every phase, produce a self-certification report with: files changed (with one line each), what you verified and how, each item from the doc 08 section 12 definition of done that applies to this phase marked pass/fail/not-yet-applicable with evidence, race conditions considered, anything you were unsure about, and anything you did not do. Then stop and wait for my go-ahead.

Acknowledge by listing the files you expect to touch and any assumption in doc 08 you think is wrong or unverifiable. Do not write code yet.
```

---

## Phase 0: Read and map (no edits)

```
Phase 0. Do not edit anything.

Read and summarize, with file and function names:
1. How WTExecutor.mq5 polls commands, handles OPEN, tracks async requests, and writes results (including atomicity of the result file). Is there any existing way to report interim phases?
2. How ea_bridge.py sends commands and reads results, including timeouts.
3. How bulk_orders.py builds and splits orders and how the engine currently does the burst open end to end, up to the point where it registers positions and starts normal operation. Identify the exact "post-open" code that both modes must share.
4. Where terminate-symbol, terminate-all, the nuclear fallback scan, graceful stop and the timeout hard stop live, and what each touches.
5. How startup reconcile works and what the repository stores.
6. Every place a per-symbol or global config field must be added so it is not silently dropped (defaults, config_manager validation, pydantic models in api/server.py, UI payload builder, UI controls).
7. How main.py installs and compiles the EA.

Then list: anything in doc 08 that conflicts with what you found, and your proposed adjustment. Stop.
```

## Phase 1: Config plumbing (no behavior change)

```
Phase 1. Implement doc 08 section 4 config plumbing only.

Add per symbol: open_mode ("burst" default, "limit_trigger") and entry_offset. Add global: armed_timeout_seconds, win_fill_deadline_ms, cancel_ack_deadline_ms, burst_mode, max_consecutive_open_failures with the defaults in doc 08. Add them everywhere in your Phase 0 list, with validation (reject unknown open_mode, entry_offset > 0, sensible ranges on the globals). Add the Open Mode dropdown and Entry Offset input to the per-symbol UI panel and include both in the payload builder and the load path. Copy Lot Setup must not copy them.

Nothing may read these fields yet. With open_mode left at burst, the bot must behave identically. Verify config round-trips through UI -> API -> file -> UI for both values, including an old config file missing the new fields.

Self-certify, then stop.
```

## Phase 2: EA arm, place, abort, init sweep

```
Phase 2. EA only: PLACING and ARMED, plus abort and init sweep. No trigger logic yet.

Implement in WTExecutor.mq5, following doc 08 sections 6 and 7:
- The ARMLIMIT command parser (header plus PB/PS/CS/CB lines, tolerant of the existing format conventions) and ABORTARM and a magic-wide CANCELALL.
- The state machine skeleton (IDLE, PLACING, ARMED, ABORTED, DONE) with microsecond phase stamps.
- Async placement of PB and PS pendings, mapping request ids to order tickets via the request-result events, confirming counts, reporting an interim ARMED phase or ABORT PLACE_SHORT (removing whatever was placed).
- Armed timeout: remove everything and report ABORT ARM_TIMEOUT.
- ABORTARM while armed: remove everything by magic (not just recorded tickets) and report ABORT USER_ABORT.
- OnInit sweep: delete any pending with the EA magic.
- The command poll and timer must keep working while armed (no blocking, busy flag must not block abort handling).
- Phased result reporting that leaves the existing OPEN result flow untouched.

Verify the pending fill policy and expiration mode the Weltrade symbols accept, and report what you found. Provide a way for me to test this phase without Python changes (for example a documented sample command file I can drop into the command path), plus exact steps to watch in MT5's orders tab.

Self-certify, then stop.
```

## Phase 3: EA trigger, cancel, burst, deadlines

```
Phase 3. EA only: complete the state machine.

Implement per doc 08 section 7:
- Trigger detection on entry-in deals for the EA magic, mapped to ladder by recorded order tickets (comment tag as cross-check). Never cancel the triggered ladder.
- CANCELLING of the losing ladder with per-remove outcome tracking. "Order not found" means possible fill, so check deals. Any losing-ladder entry deal before the burst -> do not burst, remove everything, ABORT BOTH_SIDED.
- Winner completion by deal count with win_fill_deadline_ms; on expiry remove unfilled winner orders and ABORT WINNER_SHORT.
- BURSTING for AFTER_CANCEL (start after losing removes resolve cleanly) and PARALLEL (start on trigger). Reuse the same market-order path, fill policy and invalid-stops retry as OPEN. If short after retries, ABORT BURST_SHORT.
- DONE report with counts per side and stamps: trigger, cancels acked, burst done.
- Test-only inputs TestCancelDelayMs and TestUnfillableWinner, default off, logged loudly at init when on.
- Every state ends in DONE or ABORT with a reason. No path may hang. Prove it by walking each state and each deadline in your report.

Give me a manual test script for: happy path lower trigger, happy path upper trigger, both-sided (using TestCancelDelayMs), winner short (using TestUnfillableWinner), and abort during CANCELLING and during BURSTING, each with exactly what to watch in MT5.

Self-certify, then stop.
```

## Phase 4: Python bridge and plan builder

```
Phase 4. Python bridge and plan builder only. Do not touch the engine's open path yet.

- ea_bridge.py: arm_limit(plan) returning phased results with per-phase and overall deadlines, abort_arm(id), cancel_all_pendings(symbol, magic) with the direct orders_get plus TRADE_ACTION_REMOVE fallback when the EA is unresponsive. Nothing may hang.
- bulk_orders.py: a plan builder that, from config, a fresh tick mid and entry_offset, computes the effective offset (clamped to the floor in doc 08 section 5, logging the clamp), both levels, both ladders and both contingent bursts with TP/SL from each scenario's center using the existing slot formulas, no TP/SL on constant-side lines, and reuses existing lot splitting. Include the preflight checks from doc 08 section 5 and fail loudly with specific messages.
- Add a small self-check I can run from the command line that builds a plan for each symbol and prints the levels and a summary of both scenarios, so I can eyeball the numbers against my own arithmetic before anything touches the broker.

Check your plan output against the worked example in doc 08 section 8 and state whether it matches.

Self-certify, then stop.
```

## Phase 5: Engine integration, lifecycle, persistence

```
Phase 5. Wire limit_trigger into the engine.

- Add the limit_trigger branch beside the burst branch in the open path, following doc 08 section 8. Extract the shared post-open routine if it does not exist and make both modes call it. No duplicated post-open logic.
- Flow: preflight -> persist ARMED (levels, pending tickets, cmd id) -> arm_limit -> terminal phase -> reconcile against positions_get and orders_get (per-side counts, no leftover pendings, ticket-to-slot mapping via tags) -> set center to the triggered level -> shared post-open.
- ARM_TIMEOUT re-arms around the current price. Any other ABORT or reconcile failure: sweep pendings, flatten, restart, increment the consecutive failure counter; at max_consecutive_open_failures stop the symbol with a loud reason. A success resets the counter.
- Lifecycle: graceful stop while ARMED sends ABORTARM and stops. Terminate-symbol, terminate-all (including the nuclear account scan) and the timeout hard stop all sweep pendings. Do not change the timer semantics.
- Persistence and recovery: add the ARMED phase and pending ticket set to the repository. Startup reconcile removes any pending carrying our magic, and flattens plus restarts idle if the persisted phase was ARMED or mid-open.
- Re-read doc 05 and list which of its races apply here and how each is handled.

Verify burst mode is untouched by diffing the burst branch. Then give me the manual test script for tests 1 to 8 and 11 from doc 08 section 10.

Self-certify, then stop.
```

## Phase 6: Metrics, logging, UI status

```
Phase 6. Observability.

- Activity log lines with a [LIMIT] prefix for every phase as listed in doc 08 section 8.
- After every successful open in BOTH modes, append a row to logs/users/{user}/sessions/open_quality.csv with the fields in doc 08 section 8. Compute from positions_get (price_open, time_msc) and the EA stamps. Header written once. A write failure must be logged and must never break the trading path.
- Bot State in the UI shows "Armed - waiting for trigger" while ARMED. Status payload carries what is needed. Nothing else restyled.

Show me a sample CSV row from a real demo run or, if you cannot run one, an exact description of how I produce one. Self-certify, then stop.
```

## Phase 7: Install pipeline and docs

```
Phase 7. Make sure main.py's EA install and compile step handles the changed WTExecutor.mq5, fails visibly on a compile error, and still removes nothing it should not. Update mq5_run.md with: the new open mode, the new config fields, how to switch modes, the demo test steps, how to read open_quality.csv, and the EA test-only inputs with a warning that they must be off in production. Update the VPS run steps if anything changed. Self-certify, then stop.
```

## Phase 8: Final audit

```
Phase 8. Audit, not new features.

Go through doc 06 plus doc 08 section 12 line by line and mark each pass/fail with file and line evidence. Then specifically try to break it: list every way a pending order could survive (stop, terminate, terminate-all, timeout hard stop, Python crash, EA reinit, MT5 restart, both-sided, abort during each EA state) and show the code that handles each. List every wait in EA and Python and its deadline. List anything duplicated or mirrored. Run the burst-mode regression and report. Fix only what is demonstrably wrong, listing each fix. Then give me the final manual test checklist for the VPS, including the 30-cycle dispersion comparison.
```





After each phase, make sure you commit to ONLY my new git branch which is "limit-triggers". do not add yourself as a contributor or write the "generated by codebuff" when done totally, give me a full summary of what you changed (file and function names) and anything from section 4 that you observed or could not verify. Do not mark a phase done if you could not run its check; tell me what you could not test. If something in the plan conflicts with the code you find, stop and ask me instead of guessing. Never log or print the MT5 password, and make sure the temporary ini file is always deleted.