# Limit trigger — final manual test checklist for the VPS

Work through this on the demo account before any live run. Every step says what
to look for and what a failure means, because "it looked fine" is not a result.

Prerequisite: `python3 tools/audit_limit_trigger.py` passes locally (100 checks).
That proves the code says what it should; this checklist proves the broker agrees.

---

## A. Before you start

1. Demo account. Algo Trading on (green toolbar button, plus Tools > Options >
   Expert Advisors > Allow algorithmic trading).
2. EA compiled and attached. The server log must print `EA ready v1.4`. If it
   prints a different version, stop: burst will work but limit trigger will
   refuse, by design.
3. One symbol enabled. Small counts (2 and 2) and the smallest lot. Do not use a
   real-money account for any of this.
4. `logs/users/<your-user>/sessions/open_quality.csv` does not exist yet, or you
   have noted its current line count. You will append to it.

---

## B. The burst baseline (do this FIRST)

Run burst mode for the full session you intend to spend on limit mode. You need
a like-for-like comparison, and burst is the baseline you are comparing against.

Record: number of cycles, the symbol, and roughly what time of day.

---

## C. Limit mode, happy path

1. Set Open Mode to LIMIT TRIGGER. Set Entry Offset to something comfortably
   above the symbol's minimum stop distance, or the preflight will refuse and
   tell you why.
2. Start the symbol. In the activity log you should see, in order:
   - a `[LIMIT] plan:` line with the lower and upper levels
   - `[LIMIT] preflight passed`
   - `[LIMIT] ARMED` with `placed N buy / N sell limits` matching your counts
3. The UI Bot State should read **Armed - waiting for trigger**, and the header
   badge should be amber. Nothing is open yet. This is the normal state, not a
   hang.
4. Wait for a trigger. Expect `TRIGGERED` with a side, then the cancel, then
   `DONE`.
5. **In MT5's Trade tab**, not the bot log, count the positions. You should see
   your moving count on one side and your constant count on the other, and NO
   pending orders left under the Orders tab.
6. Confirm every moving position's TP and SL against the formula for your
   `grid_distance` / `moving_freq`. Recompute two by hand. The constant
   positions must have no TP and no SL.

If any of these disagree, stop and send me the log lines. Do not go further.

---

## D. The 30-cycle comparison (the actual point of this)

1. Let 30 full cycles complete in limit mode on the same symbol, same settings,
   same time of day as the burst baseline. Fewer than 30 is anecdote.
2. Open `open_quality.csv`. For each row, look at:
   - `mode` — confirms both sets are in the same file
   - `distinct_prices` and `modal_share` per side
   - `span_ms` per side
   - `trigger_to_cancel_ms` and `trigger_to_burst_ms` (limit rows only)
3. What you are looking for:
   - **limit_trigger**: `distinct_prices` should be `1` and `modal_share` `1.00`
     on both sides. If you see more than one distinct price on a side, something
     is filling at more than the trigger price. That is a real finding; send me
     the row.
   - **burst**: several distinct prices and a modal share well below 1.00.
   - `span_ms` on burst should be visibly larger than on limit.
4. Report the median and worst-case `distinct_prices` and `modal_share` for both
   modes. That comparison is the deliverable, not a vibe.

---

## E. Every way a pending order could survive

For each of these, confirm **zero** pending orders remain for the magic
(123456) afterwards. Check MT5's Orders tab directly, filtered by magic.

| # | Scenario | How to trigger it | Expected |
|---|---|---|---|
| 1 | Graceful stop while armed | Stop the symbol while it reads "Armed - waiting for trigger" | Log shows "abort while ARMED"; no pendings |
| 2 | Graceful stop while open | Stop mid-cycle with positions open | Positions closed out, no pendings |
| 3 | Terminate | Press Kill on the symbol | Sweep before and after the close, no pendings |
| 4 | Terminate-all | Terminate all from the API/UI | Every symbol swept, no pendings |
| 5 | Session timeout | Set `max_runtime_minutes` low so it expires while armed | Hard stop sweeps, no pendings |
| 6 | Hard kill | Kill the Python process while armed, then restart | Startup reconcile sweeps, logs "persisted phase was ARMED", restarts idle |
| 7 | MT5/terminal restart | Close and reopen the terminal while armed | EA `OnInit` sweeps the magic across all symbols |

Scenarios 6 and 7 are the two most likely to be missed, because the recovery
happens before you are looking at the screen. Check the pendings *after* the
restart, not during.

---

## F. Abort timing (use the test inputs)

Set these on the EA inputs, demo only, and reset them afterwards.

1. `TestCancelDelayMs = 2000`. Trigger a limit open. Expect the cancel to be
   visibly delayed, then either a successful `DONE` or a `CANCEL_FAIL` abort.
   Either is fine; a hang is not. Confirm the abort reason is named.
2. `TestUnfillableWinner = true`. Trigger a limit open. Expect `WINNER_SHORT`
   after `win_fill_deadline_ms`, then a flatten, then a restart. Confirm no
   position from the unfilled ladder survives.
3. Both inputs back to `0` / `false`. Confirm the `*** TEST MODE` warning is
   gone from the EA log.

---

## G. Two symbols at once

Enable two symbols both in limit mode.

1. Both should arm independently. One symbol's arm must not delay the other's
   deadline, and neither should block on the other's open.
2. Trigger one symbol. The other must stay armed and unaffected.
3. Stop one symbol while the other is mid-open. Confirm the other completes
   normally.
4. Check `open_quality.csv` has rows for both symbols and the right mode on each.

---

## H. Circuit breaker

1. Stop the EA (or make it unhealthy) with a symbol in limit mode.
2. Watch the log: each failed open should count, and after
   `max_consecutive_open_failures` the symbol should stop with a loud
   `[LIMIT] STOPPED:` line naming the last reason.
3. Confirm the symbol is not left with pendings or positions.
4. Restart the EA and confirm the symbol starts fresh — the counter is in memory
   only, so a restart clears it. That is intended, not a bug.

---

## I. Burst must not have changed

Run the burst baseline from section B. Confirm the cycle behaviour, the log
lines and the position counts match what you saw before this work started. If
anything about burst shifted, that is the most important thing to report.

---

## J. What I could not verify without a terminal

Being explicit, per doc 06:

- The MQL5 compile. I cannot compile MQL5 here. `tools/verify_ea_static.py`
  checks structure and cross-file consistency, not that MetaEditor accepts it.
  Step A is the first real compile.
- `ORDER_FILLING_RETURN` behaviour on a live symbol, and whether this broker's
  `SYMBOL_EXPIRATION_MODE` offers anything useful. If pendings are rejected on
  attach, that shows up immediately at `ARMED` as `PLACE_SHORT`.
- Exact limit fill prices. Every open_quality claim above depends on the broker
  filling the whole ladder at the limit price; that is what section D measures.
- Whether `positions_get` lags deal events enough to matter. The reconcile
  retries for about a second to absorb it; section C step 5 will show if that is
  enough.
- `HistoryDealSelect` inside `OnTradeTransaction`. The deal-history safety net
  exists because some brokers report entry deals through the event stream
  late. If `dealscan_fallbacks` is non-zero in the phase logs, the safety net is
  doing real work on your broker and the win-fill deadline may need raising.
