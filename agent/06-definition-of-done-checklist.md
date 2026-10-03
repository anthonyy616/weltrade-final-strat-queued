# Queued Close Strategy — Definition of Done

Purpose of this document: the agent runs through the relevant section below *before* telling Anthony a piece of work is ready to test, not after he finds a problem on MT5. If any box in the relevant section can't be honestly checked, the work is not done yet — say so and say what's blocking it, rather than handing over something that looks finished.

## Before claiming "config schema is done"

- [ ] Every field name matches the architecture doc exactly, in the Pydantic model, the `ConfigManager` defaults, the DB (if applicable), and the UI element IDs — grep for each field name across all four locations and confirm no synonym slipped in.
- [ ] `constant_side` rejects any value other than `"buy"`/`"sell"` with a clear error, not a silent fallback to a default.
- [ ] `constant_freq >= moving_freq` is rejected or clamped, not silently accepted.
- [ ] Old fields (`tp_pips`, `sl_pips`, `sets`, `sets_config`, lot arrays, `max_positions`) are absent from the new schema entirely — not present-but-unused.
- [ ] Config can be saved and reloaded (server restart) with all new fields intact — actually restart the process and re-fetch `/config`, don't just check the in-memory object.

## Before claiming "startup logic is done"

- [ ] Opened the exact configured count on both sides in a real MT5 demo account and counted them in the Trade tab — not inferred from the bot's own log line.
- [ ] Every moving position's TP and SL, read directly from MT5's Trade tab, matches the formula's output for the actual configured `grid_distance`/`moving_freq` — recompute by hand for at least two positions and compare.
- [ ] Every constant position, read directly from MT5's Trade tab, has no TP and no SL set — confirmed by looking at the position, not by trusting the order request code.
- [ ] `up_targets` and `down_targets` in the saved state (query the DB directly) contain the correct count and correct prices for the configured inputs.

## Before claiming "tick handling / queue logic is done"

- [ ] Manually forced a moving closure (temporarily moved a TP to something reachable) and confirmed exactly one constant closed, at the price the formula predicts, and the running counts incremented correctly — done on the live terminal, watched in real time.
- [ ] Manually forced the "constant target already passed before moving closes" scenario and confirmed the constant queues rather than firing early, then fires together with the next moving closure.
- [ ] Manually forced a reversal after some closures and confirmed the opposite direction's list is used with no code change or manual intervention required.
- [ ] Confirmed no `if reversed:` or equivalent special-case branch exists anywhere in the tick-handling code — the direction-keyed lookup should be the only mechanism, per the implementation guide.
- [ ] Ran with an asymmetric count (e.g. 5/3), let one side finish first, and confirmed the other side was force-closed at that exact moment with the correct `EARLY_FORCE_CLOSE` log entry — not just that the position count reached zero eventually.

## Before claiming "retry / terminate-on-exhausted-queue is done"

- [ ] Deliberately caused a close attempt to fail (e.g. an invalid volume or a temporarily unselected symbol) and confirmed it requeues rather than being dropped silently.
- [ ] Confirmed the terminate-all-and-restart only triggers when the failing item is genuinely the last one pending, not on every failure.
- [ ] Confirmed the retry attempts one close per tick, not a busy loop — check this by adding a temporary counter/log and watching it increment once per tick, not multiple times within one tick's processing.

## Before claiming "persistence is done"

- [ ] Every new table from the architecture doc exists and is populated correctly — query the DB directly with a SQLite browser or `sqlite3` CLI, don't just trust that the save call didn't throw.
- [ ] State is saved after every mutation point listed in the architecture doc (moving closure, queue attempt, cycle end) — confirmed by checking the DB's `last_update_time`-equivalent column changes at each of those moments, not just at cycle end.
- [ ] Cycle-end (either normal or early) clears the previous cycle's `constant_queue` rows — confirmed by querying the table after a cycle ends and seeing it empty for that `cycle_id`.

## Before claiming "reconciliation / crash recovery is done"

- [ ] Actually killed the bot process (not a graceful stop — a hard kill, e.g. `taskkill` or closing the terminal) while positions were open and some closures were in progress, restarted it, and confirmed it correctly identified what closed while it was down.
- [ ] Confirmed the recovered state's running counts and queue match what a continuous, uninterrupted run would have produced for the same market movement — this requires comparing against MT5's actual deal history for that period, not just checking that the bot didn't crash on restart.
- [ ] Confirmed `catching_up` is `True` for the entire reconciliation pass and no live tick was processed by the strategy logic during that window — add a temporary log line on entry to the live-tick path during testing to make sure it wasn't invoked mid-reconciliation.
- [ ] Tested the specific case where the cycle had already ended (one side's count already reached) during the outage, and confirmed force-close-and-restart happened correctly on recovery, not just that positions were "cleaned up somehow."
- [ ] Tested a kill happening mid-`order_send` for a constant close (if reproducible) or at minimum reasoned through and documented what happens if the process dies between sending a close order and recording its result — this is the single highest-risk timing window in the whole system.

## Before claiming "UI is done"

- [ ] All new fields save correctly and reload correctly after a page refresh (not just after the same session's in-memory state).
- [ ] The constant-side dropdown only ever offers Buy/Sell, never blank, never free text, and its default matches what's actually saved in config on load — not a hardcoded default that briefly shows before `loadConfig()` runs.
- [ ] The moving/constant status readout updates in real time during a live test run and its numbers match what you can independently count in MT5's Trade tab at the same moment.

## General rule for the agent, every time

If any checklist item above can't be verified because the feature genuinely isn't built yet, that's fine — say exactly that. What's not acceptable is checking a box based on "the code looks like it should do this" without having actually watched it happen on MT5. The whole reason this document exists is that "should work" and "watched it work" have historically not been the same thing on this project, and the gap between them is what turns into a support round-trip.
