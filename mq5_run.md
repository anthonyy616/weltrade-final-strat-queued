# Running the bot on MT5

## 1. Install and compile the EA

In MT5, click File > Open Data Folder. Open the MQL5 folder, then Experts. The bot
now installs and compiles `WTExecutor.mq5` itself at startup, so on a normal run
you do not need to do this by hand. To do it manually instead:

Press F4 to open MetaEditor, find `WTExecutor.mq5` under Experts, and press F7. Look
at the Errors tab at the bottom. You want "0 errors". Warnings are fine. If there
are errors, paste them here and I'll fix them, since I haven't been able to compile
this myself.

If a compile fails at startup, the server log prints the compiler's actual error
lines rather than a bare "compile failed". The parallel path refuses to arm
with a broken EA; it never silently changes order behavior.

Back in MT5, make sure Algo Trading is on. The toolbar button should be green with
a play icon. Also check Tools > Options > Expert Advisors and tick "Allow
algorithmic trading". This setting is separate from the Python API, which is why
the bot never needed it.

Open a chart for the symbol. In Market Watch, right-click the symbol and pick Chart
Window. Make sure you're on the demo account and that your Python bot isn't running.

In the Navigator panel (Ctrl+N), open Expert Advisors, right-click and refresh if
you don't see it, and drag `WTExecutor` onto the chart. Tick "Allow Algo Trading"
on the Common tab. A smiley face in the chart's top-right corner means the EA is
active.

If the EA answers but reports an older version than the repo source, the server
prints a warning and the parallel path refuses to arm. That means a stale build
is attached: recompile and reattach.

## 2. Parallel limit-trigger opening

Every symbol uses the same parallel limit-trigger path. The EA places both
pending ladders around the current price. When either side fills, it immediately
cancels every remaining pending limit on the opposite side, re-checks until the
opposite side is clear (or the deadline expires), and only then releases the
contingent market orders. If the EA is unavailable, unhealthy, or too old, the
open fails loudly and the symbol is retried then stopped by the circuit breaker.

## 3. New settings

Per symbol, next to the existing buy/sell counts and lots:

| Setting | Meaning | Default |
| --- | --- | --- |
| Entry Offset | How far either side of the current mid price the ladders sit, in points | 50 |

Global, under the limit-trigger block:

| Setting | Meaning | Default | Range |
| --- | --- | --- | --- |
| Armed Timeout | How long to wait for a trigger before re-arming, in seconds | 120 | 10-3600 |
| Win Fill Deadline | How long the triggered ladder has to finish filling, in ms | 1500 | 100-60000 |
| Cancel Ack Deadline | How long the losing ladder's cancel has to confirm, in ms | 1500 | 100-60000 |
| Max Consecutive Open Failures | Failures in a row before the symbol stops | 3 | 1-100 |

**Entry Offset** is used by the limit path. It's raised automatically if the
broker's stop level or the symbol's minimum stop distance demands more room, and
when that happens the effective offset is logged so you can see it was clamped.

**Win Fill Deadline** and **Cancel Ack Deadline** are the two races the EA has to
win. If either expires the open aborts, everything pending is removed, any
positions are closed, and the cycle restarts. Raise them on a slow or jumpy
symbol before raising anything else.

## 4. Armed state

The timing fields are safety deadlines, not mode selectors. The armed timeout
limits how long the EA waits for the first pending fill. The win-fill deadline
limits completion of the triggering side, and the cancel-ack deadline limits the
opposite-side cleanup. A timeout aborts the cycle, removes pending orders, and
lets the engine retry under its failure breaker.

While a symbol is armed, the Bot State reads **Armed - waiting for trigger**. That
means the ladders are parked and nothing is open yet. Stopping the bot while armed
removes the pending ladders rather than leaving them on the book.

## 5. Demo test steps

Full procedure in `experts/LIMIT_TRIGGER_VPS_CHECKLIST.md` (the pre-flight and
comparison checklist) and `experts/LIMIT_TRIGGER_MANUAL_TEST.md` (the EA state
machine, one test per abort path). The short version:

1. Demo account, EA compiled and attached, Python bot stopped.
2. Set one symbol's Entry Offset at or above the symbol's minimum
   stop distance, small counts (2 and 2), and lots you don't mind losing.
3. Start the bot. Confirm the activity log shows the plan line, preflight passed,
   then `ARMED` with the placed counts matching what you configured.
4. Wait for a trigger. You should see `TRIGGERED` with a side, cancel attempts
   and `opposite side clear`, then the contingent burst and `DONE`.
5. Check the open: all positions on one side, none on the other, no leftover
   pending orders.
6. Let a full cycle close, then repeat. Do at least 30 cycles before drawing any
   conclusion (see below).

## 6. Reading open_quality.csv

One row per successful limit-trigger open at
`logs/users/{user}/sessions/open_quality.csv`. Columns:

- `mode` — `limit_trigger`.
- `trigger_side` — `lower` or `upper`.
- Per side (`buy_*`, `sell_*`): `orders`, `distinct_prices`, `min_price`,
  `max_price`, `modal_price`, `modal_share`, `span_ms`.
- `trigger_to_cancel_ms`, `trigger_to_burst_ms` — EA timings.

The comparison that matters is `distinct_prices` and `modal_share`. With parallel
limit trigger you should see `distinct_prices` of 1 and a modal share of 1.00,
because each contingent side is released from the same trigger event.

`span_ms` is the first-to-last fill time on that side. Large burst spans are the
visible cost of firing eight orders at market.

A blank cell means "not reported", not zero. An empty side has no counts at all.

## 7. EA inputs — test only

Two inputs exist for testing the failure paths and must be left at their defaults
in production:

- `TestCancelDelayMs` (default 0) — widens the window between trigger and losing
  ladder cancel, so you can watch the cancel race.
- `TestUnfillableWinner` (default false) — makes the triggered ladder never
  complete, so you can watch the win-fill deadline fire.

**Both must be 0 / false on a live account.** With either set, every limit open is
deliberately broken: the first aborts after a delay, the second never completes and
aborts at the deadline. The EA prints a loud warning at init if either is non-
default, but it will still run.

Two production inputs worth checking:

- `InpSweepMagic` (default 123456) — the magic swept for stale pending orders when
  the EA initialises. It must match the strategy's magic, or a restart will leave
  old pending orders on the book. `OnInit` is the only sweep that ignores the
  symbol and clears this magic across all symbols.
- `InpPendingFilling` (default `ORDER_FILLING_RETURN`) — pendings do not use FOK.
  `RETURN` means a pending order rests on the book until filled or expired, which
  is what a limit ladder needs. Setting it to `FOK` would reject every pending.

If something seems wrong, check the Experts tab first. Usual causes: Algo Trading
off (red toolbar button), EA on the wrong symbol, or a silent compile failure.
