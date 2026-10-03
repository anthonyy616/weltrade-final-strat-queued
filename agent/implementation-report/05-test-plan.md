# Test Plan — Demo Account Soak (Phase 10)

Maps to `agent/06-definition-of-done-checklist.md`. Each test names the checklist section it satisfies. Run all of this on a **demo account** with a small pool first (2/2, 0.01 lots).

**Before starting:** rebuild the environment on the `test` branch, ensure `.env` points at the Weltrade demo MT5 terminal, start the server (`python main.py`).

> **D2 warning:** `/control/start` deletes the DB. Config survives (JSON files), but any mid-cycle recovery state does not. Tests 1–4 are unaffected; keep it in mind for Test 7.

---

## Test 1 — Config schema (DoD: "config schema is done")

1. Open the UI, log in, enable FX Vol 20. Confirm the panel shows exactly: Buy Count, Sell Count, Buy Lot, Sell Lot, Constant Side (select), Grid Dist, Moving Freq, Constant Freq — no TP/SL/sets fields anywhere.
2. Save, then **fully restart the server** and reload the page. All values must survive.
3. Try `constant_freq >= moving_freq` (e.g. moving 10, constant 12) → server clamps constant to 9.0 and prints a `[CONFIG]` warning; reload the page and confirm the clamped value is displayed.
4. Check the old config file was migrated: `config_<user_id>.json` should contain only the new fields (no `tp_pips`, `sets_config`, etc.).

## Test 2 — Startup (DoD: "startup logic is done")

Configure 2 buys / 2 sells, `constant_side: sell`, 0.01 lots. Press Start on the symbol.

- [ ] MT5 Trade tab shows exactly 2 BUY + 2 SELL positions (count them, don't trust the log).
- [ ] Every BUY has TP and SL set; every SELL has **no TP and no SL** (read the position rows in MT5, not the bot log — agent/03's "silently accepted stops" trap).
- [ ] Hand-verify the math for at least two positions: note the start price from the log (`Entry price:` line), then TP_n = start + grid_distance + n×moving_freq, SL_n = start − grid_distance − n×moving_freq for the buys. Compare against MT5's displayed TP/SL. (Formulas in `agent/02` §6; the engine uses raw price units.)
- [ ] Query the DB: `moving_positions` has 4 rows with correct slots; `constant_targets` has 4 rows (2 up + 2 down) with correct prices; `symbol_state.metadata` has the config snapshot.

## Test 3 — Closure → queue → constant close (DoD: "tick handling / queue logic")

Leave the bot running from Test 2.

- [ ] Force a moving closure: in MT5, manually modify one BUY's TP to just above current price. Watch: the position closes → exactly ONE sell closes → the log shows the constant close with the releasing ticket and target (`down#1 @ …` or `up#1 @ …`).
- [ ] Status readout shows `Moving 1/2 · Constant 1/2`.
- [ ] Repeat for the second BUY → `Moving 2/2 · Constant 2/2` → cycle-end fires (both sides equal → `NORMAL`), log shows `Cycle #1 COMPLETE | Reason: Both sides reached…`, all positions closed, and a new cycle opens at current price (2/2 again, `cycle_count` = 2).

## Test 4 — Reversal (DoD: same section, reversal box)

Start a cycle, let one direction's first closure happen naturally (or force it), then force price the other way (demo: force the second closure by moving the other side's TP).

- [ ] The second closure pulls from the **opposite** direction's list with no intervention — log line shows the other direction's target.
- [ ] Confirm in the engine log there is no "reversal" message anywhere (there is no such code path — if the wrong price released, check `up_targets`/`down_targets` separation, per agent/03).

## Test 5 — Asymmetric counts + EARLY_FORCE_CLOSE (DoD: same section)

Configure 4 buys / 2 sells (constant_side sell → moving=buy ×4, constant=sell ×2). Start.

- [ ] Force 2 moving closures (2 buys closed). Constant count is now 2/2 → cycle ends immediately even though 2 buys remain open.
- [ ] The 2 remaining buys are force-closed **at that exact moment** — verify timestamps in the activity log and position disappearance in MT5.
- [ ] Log shows `EARLY_FORCE_CLOSE` / "One side finished first".

## Test 6 — Retry & terminate-on-last-item (DoD: "retry / terminate-on-exhausted-queue")

1. Start a cycle. Before any closure, in MT5 temporarily disable trading on the symbol or set an invalid state that makes order_send fail (simplest: change the symbol's volume step so 0.01 closes fail — or use the MT5 "AutoTrading" global toggle OFF, which rejects order sends).
2. Force one moving closure.
- [ ] Log shows `Constant close failed … — retry 1` each tick, one attempt per tick (retry count increments once per tick, never multiple times within a tick).
- [ ] With AutoTrading off, the item keeps requeueing (not dropped).
3. Re-enable AutoTrading → the queued constant closes on a later tick.
- [ ] For terminate-on-last-item: with AutoTrading off, force closures until only ONE constant remains queued → within a tick or two the bot terminates all and starts a fresh cycle (`TERMINATE_QUEUE_EXHAUSTED` in the log).

## Test 7 — Hard-kill reconciliation (DoD: "reconciliation / crash recovery")

Setup: start a cycle, force 1–2 moving closures so the queue has fired, leave the rest running. Then **kill the process hard** (Ctrl+C is graceful-ish; use `kill -9` / close the terminal) *while a moving position is inside its TP/SL zone or right after manually closing one in MT5 while the bot is down*.

1. While the bot is down: manually close one more moving position in MT5 (or let a TP fire).
2. Restart the process **without pressing Start All** (bot restore triggers `reconcile_strategies_on_startup`; pressing Start All deletes the DB — D2).
- [ ] Log shows `[RECOVERY] Replaying closure of moving #<ticket> (TP/SL @ price)` for each closure that happened while down.
- [ ] Counts after restart match MT5's deal history for the outage window — compare closed counts against the MT5 History tab, not the bot's claim.
- [ ] The corresponding constant(s) closed exactly once each (no double-fire — check MT5 history for duplicate closes at the same target price).
- [ ] If the kill landed such that a side's total was already reached: the force-close-and-restart ran on recovery (look for `Cycle #n COMPLETE` right after the recovery lines).
- [ ] Add a temporary log line at the top of `on_external_tick`'s lock block during this test and confirm **no** live tick processed between the recovery lines (agent/06 catching_up box).

## Test 8 — Terminate semantics (DoD: lifecycle rows)

- [ ] Mid-cycle with a non-empty queue: press the symbol's **Kill** → all positions gone in MT5, `constant_queue` table empty for the symbol, status Idle. Start again → clean cycle with no stale queue effects (agent/05's terminate-during-drain warning).
- [ ] Graceful stop: press Stop mid-cycle → no new cycle after the current one ends; bot stops after the cycle completes. Test `max_runtime_minutes` (set 2) → same behavior on timeout, with the 5-min hard-stop failsafe untouched.

## Test 9 — Multi-symbol isolation (DoD: Phase 8 box)

Enable two symbols (e.g. FX Vol 20 + FX Vol 40), start both.
- [ ] Independent counts/queues per symbol in `/status` → `strategies.{symbol}`.
- [ ] Starting one symbol shows the documented brief pause across symbols while its pool opens (blocking `order_send`, agent/05's expected-symptom note — don't panic, don't "fix" it).
- [ ] No cross-contamination in the DB (per-symbol rows filtered correctly).

## Test 10 — UI specifics (DoD: "UI is done")

- [ ] Constant-side dropdown: only Buy/Sell, no blank, shows the **saved** value on load (refresh the page mid-test and check it doesn't flash a wrong default).
- [ ] Moving · Constant readout matches MT5's Trade tab at the same moment (count manually).
- [ ] Copy Config: set symbol A's fields, copy to B, reload page — B matches A.

---

## When something breaks

Use the shape from `agent/07` Phase 10. First check `agent/05-mistakes-and-race-conditions.md` for a matching failure mode — the docs list the historically expensive ones (double queue release, ticket identity mismatch, wrong OUT deal, stale queue rows, busy-loop retries). Also read `02-design-decisions-and-deviations.md` (J6/J8 in particular) before reporting, so the known judgment calls aren't mistaken for bugs.

Report template:

```
Bug report for [symbol / test #]:
What I configured: [counts, lots, constant_side, grid_distance, moving_freq, constant_freq]
What I expected: [reference the formula/policy]
What actually happened: [exact MT5 prices/counts + timestamps]
What the logs said: [activity log + terminal lines from that moment]
DB state at that moment: [moving_positions / constant_targets / constant_queue rows]
```
