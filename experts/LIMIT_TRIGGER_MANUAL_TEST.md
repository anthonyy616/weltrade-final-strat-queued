# Manual test script — limit-trigger EA (plan phase 3)

Phases 1-3 are complete: the EA can place a ladder, detect the trigger, cancel
the losing ladder, burst the opposite side and report `DONE`. **Python does not
drive any of this yet** — that is phase 5. These tests use command files only.

Close the Python bot before every test. Never leave both running.

## Setup

1. Attach the EA to the symbol's chart, **Allow Algo Trading** ticked.
2. Toolbox (Ctrl+T) → **Experts** tab is your log. Watch `[LIMIT]` lines.
3. Toolbox → **Trade** → **Orders** tab is the source of truth for pendings.
4. Command files go in MT5's **Common\Files** folder as `wt_cmd.txt`. The EA
   deletes each one as it picks it up.
5. Phase files are `wt_arm_<id>.txt` in that same folder. Watch the one for the
   `id` you sent.

Every test below: set `armed_timeout_ms=60000` (1 minute) unless stated, use a
**wide** spread so nothing triggers accidentally, and use small counts
(2 and 2) so the Trade tab is readable.

---

## Test 1 — happy path, LOWER trigger (buys)

`moving_side` does not exist yet at this phase, so just check the mechanics.

```
id=low1
action=ARMLIMIT
symbol=FX Vol 20
magic=123456
lower=<live bid minus ~200>
upper=<live ask plus ~200>
armed_timeout_ms=60000
win_fill_deadline_ms=3000
cancel_ack_deadline_ms=3000
burst_mode=AFTER_CANCEL
PB|1|0.01|0.00|0.00|low1PB1
PB|2|0.01|0.00|0.00|low1PB2
PS|1|0.01|0.00|0.00|low1PS1
PS|2|0.01|0.00|0.00|low1PS2
CS|1|0.01|0.00|0.00|low1CS1
CS|2|0.01|0.00|0.00|low1CS2
```

Then let price fall until it reaches `lower`.

**Watch for, in this order:**
1. `wt_res.txt` → `accepted=1 reason=ARMING`, within milliseconds.
2. Orders tab → 2 buys at `lower`, 2 sells at `upper`, comments `low1PB*`/`low1PS*`.
3. `wt_arm_low1.txt` → `phase=ARMED`.
4. On the fill: `phase=TRIGGERED`, then a `[LIMIT] triggered ... side=1` log
   (side 1 = the buy ladder, which is the lower one).
5. Orders tab → the two `low1PS*` sells **disappear** and stay gone.
6. Orders tab (Positions) → 2 new sells from the burst, comments `low1CS*`.
7. `wt_arm_low1.txt` → `phase=DONE`, with `placed_pb=2`, `burst_expected=2`.
8. Orders tab → **no pendings left**.

**Accept:** exactly 2 buys at one price, 2 sells, zero pendings, `DONE`.

**Clean up:** close the 4 positions by hand.

---

## Test 2 — happy path, UPPER trigger (sells)

Same file with `id=up1` and the sides swapped: `PS` are the ladder that fills,
`CB` is the burst line. Let price rise to `upper`.

**Watch for:** `triggered ... side=2`, the `PB` buys removed, `CB` market buys
opened, `phase=DONE`, no pendings.

---

## Test 3 — both-sided (forced with TestCancelDelayMs)

Set the EA input **`TestCancelDelayMs = 3000`** and re-attach the EA. The log
must warn `[LIMIT] *** TEST MODE`.

Use a **tight** level — 5 points either side of the live price — and a bursty
symbol. Use `FX Vol 99`.

Then let price punch through `lower` and immediately reverse back through
`upper` inside the 3-second window.

**Watch for:** after `TRIGGERED`, the `PS` removes are *deliberately delayed*,
price takes out `upper`, and the phase file flips to
`phase=ABORT reason=BOTH_SIDED`.

**Accept:** **no burst**. Both sides may hold positions — the EA removes
pendings only, it never flattens. Confirm the Orders tab is empty and close the
positions by hand. A `CS`/`CB` market order appearing here is a **bug** — tell
me immediately.

**Reset `TestCancelDelayMs` to 0 and re-attach before the next test.**

---

## Test 4 — winner short (forced with TestUnfillableWinner)

Set **`TestUnfillableWinner = true`** and re-attach. It pushes the last order of
each ladder far past the level so the winner can never finish filling.

Arm normally, let price trigger `lower`, and do **not** let it come back.

**Watch for:** the ladder fills, `TRIGGERED`, the losing ladder is cancelled,
the burst fires, and then after `win_fill_deadline_ms`:
`phase=ABORT reason=WINNER_SHORT`.

**Accept:** the Orders tab is empty and the unwon orders are gone.

**Reset to `false` and re-attach.**

---

## Test 5 — abort during CANCELLING

Arm with `TestCancelDelayMs = 5000`, trigger it, and while it is in
`CANCELLING` (log shows `[LIMIT] cancelling ...`) write a second command file:

```
id=anything
action=ABORTARM
symbol=FX Vol 20
magic=123456
```

**Watch for:** `wt_res.txt` → `accepted=1 reason=ABORTING`, phase file →
`phase=ABORT reason=USER_ABORT`, Orders tab empty **within about a second** —
even though the machine was mid-cancel. No burst should ever fire.

---

## Test 6 — abort during BURSTING

Arm with `burst_mode=AFTER_CANCEL`, trigger it, and as soon as the log shows
`[LIMIT] triggered`, send the same `ABORTARM` file.

**Accept:** `phase=ABORT reason=USER_ABORT`, Orders tab empty. Some burst
positions may already exist — that is expected and correct, the race is real.
The important part is **zero pendings left**.

---

## Test 7 — deadline sanity (no fills, no trigger)

Arm with a wide spread and `armed_timeout_ms=10000`.

**Watch for:** exactly one `phase=ABORT reason=ARM_TIMEOUT` at ~10 s, Orders tab
empty, and the log line `[LIMIT] machine released ...`.

That last line matters: it proves the machine gave its slot back, so the symbol
can be armed again.

---

## Test 8 — two symbols armed at the same time

Arm `FX Vol 20` and `FX Vol 40` before either triggers, with wide spreads and
`armed_timeout_ms=60000`.

**Watch for:** two separate `wt_arm_<id>.txt` files, each with its own phase,
each with its own deadline, and **neither** stealing the other's pendings. Then
trigger one and confirm only that one cancels and bursts.

This is the multi-symbol test the whole per-symbol-machine design exists for.
If one symbol's cancel touches the other's orders, that is a bug in sweep scope.

---

## What to report back

1. Which fill policy worked: `ORDER_FILLING_RETURN` (default), `FOK` or `IOC`?
   If you see `PLACE_SHORT`, the phase file's `reason` plus the
   `[LIMIT] request failed ... retcode=` lines in the log will say why.
2. `dealscan_fallbacks` in the phase file. If it is non-zero,
   `HistoryDealSelect` is failing inside `OnTradeTransaction` on this broker and
   I want to know.
3. Whether limit fills landed at **exactly** the limit price on these synthetics.
4. Any `CANCEL_FAIL` or `BURST_SHORT` with the retcode lines around it.

## Remember

`TestCancelDelayMs` and `TestUnfillableWinner` must both be back at their
defaults (`0` and `false`) before the bot is ever run for real. The EA warns at
init if either is on — check that the warning is absent.