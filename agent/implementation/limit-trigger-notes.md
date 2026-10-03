# 10 - Limit-Trigger Open Mode: Notes for Me

## What was decided and why

- It is a second open mode, per symbol, and burst stays the default. If limit-trigger turns out no better on the VPS, I flip a dropdown and lose nothing.
- The EA owns the trigger, cancel and opposite-side burst, because the EA only talks to Python through a command file. Sending "first fill happened" up to Python and a decision back down would throw away the latency this mode exists to save. The EA only holds state for the length of one open, and Python still owns all strategy state.
- Python never believes the EA blindly. After every open it counts what is actually in MT5.
- Every failure ends the same way: remove pendings, close everything, restart the cycle. After 3 failed opens in a row the symbol stops and says why, so it cannot loop and eat spread.
- Cancel before burst by default, because pending orders count against the symbol's volume limit and the burst could be rejected otherwise.
- I did not touch the timer. Max runtime still works as it does today. Stopping while armed just cancels the pendings.
- Not built on purpose: the stop-order straddle variant. Revisit only if the account's pending order cap is generous.

## How it works now

1. Press Start on a symbol set to limit_trigger. The bot checks the account can take it (pending order cap, volume limit, offset not below the stops level) and refuses with a clear message if not.
2. It places all buy limits at price minus offset and all sell limits at price plus offset in one burst.
3. Price touches one level. That ladder fills inside the broker on one tick.
4. The EA cancels the other ladder and bursts the opposite side at market.
5. Python counts positions and orders, sets the cycle center to the level that triggered, and the normal strategy takes over.
6. If neither level is touched for the armed timeout, it cancels and re-arms around the new price.

The honest expectation: the triggered side should land at one price. The market side is better than before (half the burst, no waiting on Python) but is still best effort, and buys and sells still differ by roughly the spread. The users' "everything at one exact price" is not fully reachable, and I should say that to them plainly.

## How I know it will work and not leave a mess

Built-in protections:
- Preflight stops the known broker rejections before anything is placed.
- Every EA state ends in done or an abort with a named reason, and every Python wait has a deadline, so nothing hangs.
- No path leaves a pending order behind: stop, terminate, terminate-all, timeout stop, Python crash, EA reinit and MT5 restart each have a handler, and there is a server-side expiry as a last resort net.
- The agent has to prove each of these in its final audit, line by line.

What I should do myself, in this order, on the demo first:
1. Run the 13 tests in doc 08 section 10. The two that matter most are the both-sided race (test 9) and the winner-short case (test 10), which the EA test flags let me force on demand.
2. Always check the MT5 orders tab after stop, terminate and restart tests. It must be empty.
3. Run a burst-mode cycle after everything is built to confirm nothing regressed.
4. Run 30 cycles of each mode on the VPS and read `open_quality.csv`.

Suggested go/no-go for the users, decided by the CSV and not by feel: on the triggered side nearly every order should share one price, and the market side should show a clearly higher modal share and a shorter fill time span than burst mode does on the same symbol. If it is not measurably better after 30 cycles per mode, keep burst as the default and treat limit-trigger as an option for symbols where it does help.

## What could still go wrong

These are things only the real broker can answer, so treat the first demo runs as discovery:
- Whether Weltrade limit fills land at exactly the limit price on these synthetics.
- The pending order cap and the per-symbol volume limits. A low cap makes the mode unusable on big counts.
- Which fill policy and expiration mode the symbols accept for pendings.
- Both-sided and winner-short cases will happen sometimes, especially on FX Vol 99 with a tight offset. They are handled by flattening and restarting, which costs a spread each time. If the CSV or logs show many, widen the offset.
- A wider offset makes both levels safer but means more waiting before a cycle starts. That tradeoff is mine to tune per symbol.

Never leave the EA test inputs on in production. The EA logs a warning at init when they are on.