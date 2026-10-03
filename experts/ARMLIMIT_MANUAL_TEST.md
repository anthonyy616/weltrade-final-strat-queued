# Testing the ARMLIMIT command by hand (no Python needed)

This is for plan phase 2 only: the EA places pending ladders, arms, and aborts.
It does **not** yet detect a trigger, cancel the losing ladder or burst the
opposite side — that is phase 3.

## Where the file goes

Copy `sample_armlimit_cmd.txt` into the MT5 terminal's **Common\Files** folder as
`wt_cmd.txt`. The folder path is printed by the EA at startup and by
`tools/ea_doctor.py`. It is normally:

    %APPDATA%\MetaQuotes\Terminal\Common\Files

The EA deletes `wt_cmd.txt` as soon as it picks it up, so you only need to write
it once per test.

## Before you start

1. Close the Python bot. Do not run both at once.
2. Attach the EA to the symbol's chart with **Allow Algo Trading** ticked.
3. Open the Toolbox (Ctrl+T), **Experts** tab — that is your log feed.
4. Put a wide spread around the live price so the armed timeout fires predictably.

## Set the levels

`lower` and `upper` must bracket the live price and satisfy the broker's stops
level. For FX Vol 20 around 9000, something like:

    lower=8980.00
    upper=9020.00

`PB` (buy limits) are placed at `lower`. `PS` (sell limits) are placed at
`upper`. `TP`/`SL` of `0.00` means no stop — which is what you want here.

## Test 1 — arm and time out (the main phase-2 path)

1. Write the command file.
2. Within ~5 ms the EA answers in `wt_res.txt` with `accepted=1 reason=ARMING`.
3. Open MT5's **Trade → Orders** tab. You should see your `PB` orders sitting at
   `lower` and your `PS` orders at `upper`, all tagged `armtPB001` etc.
4. Open `wt_arm_<id>.txt` in the same folder. `phase=ARMED` and the counts are there.
5. Wait for `armed_timeout_ms`. The phase file flips to `phase=ABORT
   reason=ARM_TIMEOUT`, and **the orders tab must be empty within a second**.

## Test 2 — abort while armed

1. Write the command file again (bump `id` so you get a fresh phase file).
2. While it is `ARMED`, overwrite `wt_cmd.txt` with the same file but
   `action=ABORTARM`.
3. The phase file must show `phase=ABORT reason=USER_ABORT`, and the orders tab
   must be empty.

## Test 3 — rejections

Change one thing at a time and check `wt_res.txt` for `accepted=0`:

| Change | Expected `reason` |
|---|---|
| `lower=0.00` and `upper=0.00` | `BAD_LEVELS` |
| `lower` above `upper` | `BAD_LEVELS` |
| symbol you do not have | `SYMBOL_UNAVAILABLE` |
| delete every `PB`/`PS` line | `NO_PLACABLE_ORDERS` |
| send ARMLIMIT twice for the same symbol before the first finishes | `SYMBOL_ALREADY_ARMED` |

## Test 4 — OnInit sweep

1. Arm something (Test 1) and then, before the timeout fires, **remove the EA
   from the chart and re-attach it** (or restart MT5).
2. `OnInit` sweeps every pending carrying magic 123456. The log shows
   `[LIMIT] OnInit swept N stale pending order(s)`, and the orders tab is empty.

## What phase 2 deliberately does NOT do

- **It never triggers.** If price touches a level and your ladder fills, the
  machine still sits in `ARMED` until the timeout and then reports `ARM_TIMEOUT`.
- **It never flattens.** A ladder that filled leaves a real position behind. The
  EA only removes *pending* orders. Close those positions by hand after this
  test — the phases-3+ abort path is still Python's job to flatten.

## Fill policy and expiration — please report back

Phase 2 could not verify these from code alone. Watch for them:

1. **Fill policy.** The EA sends pendings with `ORDER_FILLING_RETURN` (input
   `InpPendingFilling`). The existing market path uses `ORDER_FILLING_FOK`, which
   pendings generally reject. If every `PB`/`PS` comes back `PLACE_SHORT`, check
   `wt_ea.log` for the retcode and try `InpPendingFilling = ORDER_FILLING_FOK`
   or `ORDER_FILLING_IOC` instead, and tell me which one works.
2. **Expiration.** `ORDER_TIME_GTC` is used. A server-side expiry was left out
   because the symbols' expiration mode is unverified — tell me what
   `SYMBOL_EXPIRATION_MODE` reports for these synthetics.

## Safety

`TestCancelDelayMs` and `TestUnfillableWinner` are phase-3 inputs and do nothing
yet. They default off, and the EA logs a loud warning at init if either is on.
Never run the bot with either enabled.