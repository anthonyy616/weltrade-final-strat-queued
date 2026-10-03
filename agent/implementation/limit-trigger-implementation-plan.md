# 08 - Limit-Trigger Open Mode: Implementation Plan

Status: in build. Applies to the queued-close fork only (not the Grid Bounce forks).
Read first: docs 01-07, especially 05 (mistakes and race conditions) and 06 (definition of done).

**Revisions (Phase 0 review, accepted by Anthony).** Where this doc and the real code
disagreed, the code won and this doc was corrected:
- EA install/compile lives in `core/ea_provisioner.py`, not `main.py` (section 8).
- The offset floor is a single formula with no `safety_margin` knob (section 4/5).
- Target computation is a pure function of center, called at plan time AND after the
  terminal phase; only the post-trigger call is persisted (section 8).
- Reconcile after `DONE` retries for ~1 s before declaring a mismatch (section 8).
- Pending tickets are NOT persisted; recovery sweeps by symbol+magic (section 8).
- Per-symbol arm machines and per-symbol phase files, outside `busy` (sections 6/7).
- Sweeps and aborts are scoped to symbol+magic because the magic is shared; only
  `OnInit` sweeps magic-wide (section 7).
- Preflight counts pendings account-wide, not per symbol (section 5).
- No shared post-open routine exists yet; it is extracted from `_try_bulk_open`
  (section 8).
- `limit_trigger` never falls back to the sequential path; an unavailable EA refuses
  to arm (section 8).
- Config validation uses the existing clamp-and-warn style (section 4).

## 1. What we are building

A second way to open a cycle, selectable per symbol, next to the existing async burst.

Today, at cycle start the bot bursts every buy and every sell at market. In the new mode:

1. At start price P, place N buy limits at `P - offset` (the lower level) and N sell limits at `P + offset` (the upper level), all in one async burst.
2. Whichever ladder is touched first fills inside the broker against a single tick. That is the "triggered side".
3. The EA immediately cancels the other ladder, then fires an async market burst for the opposite side so it opens at roughly the same price as the triggered side.
4. When everything is open, Python takes over exactly as it does after a normal burst open. Center price for the cycle is the level that triggered (lower or upper), not P.

Why: the triggered side fills server-side with no client latency, and only one side needs a fast client burst. What it does not do: make both sides the same price. Buys fill at ask, sells at bid, so a spread-sized gap remains. The market side is still best-effort.

## 2. Non-goals (do not build these)

- No "straddle" variant (stop orders staged beyond the limits). Possible v2 only if the account pending-order cap allows it.
- No change to the closing logic, queue logic, constant/moving formulas, or crash-recovery replay for open positions.
- No change to the max-runtime timer semantics. It stays engine-level.
- No refactor of unrelated code. The existing burst mode must behave exactly as before and stays the default.

## 3. Key decisions (and why)

1. **The EA owns the trigger sequence.** Python talks to the EA through a command file, so routing "first fill -> cancel -> burst" through Python would add poll plus decision latency exactly where it hurts. The EA runs a small event-driven state machine for the duration of one open only. It holds no strategy state beyond that.
2. **Python still owns strategy state and trusts nothing the EA says.** After the EA reports done, Python reconciles against `positions_get` and `orders_get`, same as the current bulk open.
3. **Both TP/SL scenarios are precomputed by Python and sent up front.** TP/SL are absolute prices anchored on the center, and the center for each scenario is simply its level, known before anything fills. The EA never computes prices.
4. **Cancel-before-burst is the default**, because pending orders count toward the symbol's per-direction volume limit. A config flag allows firing the burst in parallel where the volume math has headroom.
5. **Every failure converges to one safe action:** remove all pendings, flatten any positions, restart the cycle. This matches the existing "bulk open came up short, abort clean" decision. A circuit breaker stops the symbol after N consecutive failed opens so it cannot loop and bleed spread.
6. **Armed-with-pendings after any restart means cancel and go idle.** No attempt to resume an armed state.
7. **Fail loudly on preflight problems.** No silent fallback to burst mode.
8. **Burst mode is untouched and remains the default.** Limit-trigger is opt-in per symbol, so it can be compared on real data and switched off instantly.
9. **One arm machine per symbol, outside `busy`.** Users run several symbols at once and the EA has exactly one command slot, one result slot and one `busy` flag. `ARMLIMIT` returns an immediate accepted/rejected ack through the normal roundtrip and clears `busy`; the armed machine then runs entirely outside `busy` so it never blocks — and is never blocked by — another symbol's `OPEN`/`CLOSE`.
10. **`OPEN` and `CLOSE` stay byte-for-byte unchanged.** They keep the shared command slot, the shared result file and the `busy` flag. Arming adds a parallel path, it does not reshape the existing one.
11. **Python sends nothing for a symbol while it is armed except `ABORTARM`.** The contention surface for an armed symbol is exactly one command.

## 4. Config additions

Per symbol (same unit as `grid_distance`, which is price units as the existing TP/SL math uses):

- `open_mode`: `"burst"` (default) or `"limit_trigger"`
- `entry_offset`: distance from start price to each ladder. Effective offset is clamped **up** to the floor at arm time, and the clamp is logged loudly when it happens. See the floor formula in section 5 — there is no separate `safety_margin` knob.

Global:

- `armed_timeout_seconds` (default 120): if neither ladder is touched, cancel and re-arm around the new price.
- `win_fill_deadline_ms` (default 1500): after the first fill, how long the triggered ladder has to finish filling.
- `cancel_ack_deadline_ms` (default 1500): how long to wait for the losing ladder's removes to resolve.
- `burst_mode`: `"AFTER_CANCEL"` (default) or `"PARALLEL"`.
- `max_consecutive_open_failures` (default 3).

These fields must be added in every place a config field lives: defaults, `config_manager` validation, the pydantic models in `api/server.py` (unknown fields are silently dropped otherwise), the UI payload builder, and the UI controls. Copy Lot Setup does not copy these.

Validation follows the **existing clamp-and-warn style** in `ConfigManager._validate_symbol_fields`
(what `constant_side`, `buy_count` and `moving_freq` already do), not hard rejection:

- An unrecognised `open_mode` degrades to `"burst"` with a warning. It must **never**
  degrade to `limit_trigger` — a typo must not silently arm a new mode.
- An invalid `entry_offset` (<= 0, non-numeric, NaN) falls back to its default with a
  warning. The stops/freeze floor is applied at arm time regardless of what is stored.
- Globals clamp to their documented ranges with a warning, same style.
- Nothing reads any of these fields until Phase 5. With `open_mode` left at `burst` the
  bot must behave identically to before.

### Offset floor (single floor, no safety margin)

    floor = max(MIN_STOP_PIPS_PER_ASSET[symbol], live trade_stops_level * point)
            + current spread

`MIN_STOP_PIPS_PER_ASSET` comes from `MAX_CONFIGS.json` and is already the per-asset
minimum stop distance this codebase enforces in `bulk_orders._check_min_stops`. Reusing it
means one floor for the whole system instead of two. `_check_min_stops` keeps its existing
behaviour for TP/SL on market orders; the floor above is specifically for ladder levels.

## 5. Pre-flight checks (Python, before arming)

Refuse to arm and raise a clear error if any of these fail:

- `account_info().limit_orders` (cap on pending orders): needs room for `2 * (orders per ladder after lot splitting))` **plus every pending already on the account**, not just this symbol's. The cap is account-wide, so another symbol's armed ladder counts against it. If the cap is 0, treat as unlimited.
- Offset at or above the floor: clamp up to the section 4 floor, log both values.
- Per-direction volume: buy ladder volume must be within the symbol volume limit, and sell ladder likewise (pendings count). In `PARALLEL` mode also check pendings + contingent burst volume together.
- Per-order volume: split with the existing max-lot splitting before building the ladders.
- Both levels respect the stops level and freeze level against a fresh tick. If price moved and a level is now too close, recompute once, then fail.
- EA reachable and idle. **If the EA is unavailable, `limit_trigger` refuses to arm** — it must never silently fall back to the sequential open path. Burst mode keeps its existing sequential fallback unchanged.
- The symbol's magic is not currently armed on another machine (one arm machine per symbol per EA; a second ARMLIMIT for an already-armed symbol is rejected with a clear reason).

## 6. Protocol: ARMLIMIT command

New command, separate from `OPEN`. Mirror the existing line format, atomic file handling and id scheme in `WTExecutor.mq5` and `ea_bridge.py`. Field names below are logical, not literal.

Header: command id, symbol, magic, `lower_price`, `upper_price`, `armed_timeout_ms`, `win_fill_deadline_ms`, `cancel_ack_deadline_ms`, `burst_mode`.

Lines:
- `PB|slot|volume|tp|sl|tag` pending buy limit, placed at `lower_price`
- `PS|slot|volume|tp|sl|tag` pending sell limit, placed at `upper_price`
- `CS|slot|volume|tp|sl|tag` contingent market sells, fired if the lower level triggers (buys filled)
- `CB|slot|volume|tp|sl|tag` contingent market buys, fired if the upper level triggers (sells filled)

Python has already split lots, so each line is one broker order. Tags must fit the 31-char MT5 comment limit and carry enough to map a ticket back to a slot and role (cmd id short form, slot, role).

TP/SL of 0 means none (constant-side orders carry none).

Other commands needed: `ABORTARM` (id) to cancel everything armed on that symbol, and `CANCELALL` for a pending sweep. **Both are scoped to symbol+magic** — the magic is shared across symbols, so a magic-wide sweep would cancel another symbol's live armed ladder. The only exception is `OnInit`, where nothing legitimate can be waiting.

### Acknowledgement and phase files

`ARMLIMIT` **returns an immediate accepted/rejected ack through the normal roundtrip and clears `busy`**, exactly like `OPEN` does today. The command slot, the result slot and `busy` are therefore never held for the armed window.

Each phase after the ack goes to its own **per-symbol phase file**, written atomically (temp + rename, mirroring `WriteResult`) and read **non-destructively** by Python — Python polls the same file repeatedly and must never delete it between polls. File name derives from the command id, so two armed symbols can never collide on one path. The existing single `wt_res.txt` is untouched.

Results: the EA reports phases to the per-symbol phase file described above, not just one final answer. The existing `OPEN` result flow is untouched. Phases:

- `ARMED` (counts of pendings confirmed on each side, tickets)
- `TRIGGERED` (side, microsecond stamp)
- `DONE` (final counts per side, timing stamps: trigger, cancels acked, burst done)
- `ABORT` with a reason: `PREFLIGHT_FAIL`, `PLACE_SHORT`, `ARM_TIMEOUT` (not an error, means re-arm), `BOTH_SIDED`, `WINNER_SHORT`, `CANCEL_FAIL`, `BURST_SHORT`, `USER_ABORT`, `EA_REINIT`

`ARMED`/`TRIGGERED` are interim and `DONE`/`ABORT` are terminal. Python polls until a terminal phase or an overall deadline.

## 7. EA state machine

States: `IDLE -> PLACING -> ARMED -> (TRIGGERED) CANCELLING -> BURSTING -> DONE`, with `ABORTED` reachable from every state.

**Topology: one machine per symbol, in a small fixed-size array, serviced before the `busy` check.** The array is indexed by symbol (or by a free slot); each entry carries its own symbol, magic, cmd id, phase, phase file, request table, per-ladder order-ticket sets, deadlines and microsecond stamps. An entry not in use is `IDLE` and costs nothing.

Hard rules:
- **No blocking waits anywhere.** Deadlines and abort handling run off events and the timer.
- **Armed machines must not touch `busy`, and `busy` must not gate them.** `OnTimer` currently returns early when `busy` is set; arm machines are serviced **before** that check so another symbol's in-flight `OPEN` can never delay an armed symbol's deadline or its `ABORTARM`. Likewise `OnTradeTransaction` currently early-returns on `!busy`; the new handler must run first and demultiplex.
- **Demultiplexing.** `OnTradeTransaction` resolves a transaction to a machine by (a) request id → that machine's request table, or (b) order ticket → that machine's per-ladder ticket set, both checked under symbol+magic. Transactions matching no armed machine fall through to the existing `FindReq`/`p_id` path unchanged.
- All sends and removes use async calls. Results come back through `OnTradeTransaction` request results; map request ids to order tickets.
- **PLACING:** async-send all `PB` and `PS` orders as pendings (GTC, or a server-side expiration of armed timeout plus a margin as a dead-man switch if the symbol's expiration mode allows it). Record request ids and resulting order tickets. When every request has resolved, if the confirmed counts match expected, report `ARMED`, else `ABORT PLACE_SHORT` (remove whatever was placed).
- **ARMED:** watch `TRADE_TRANSACTION_DEAL_ADD` for entry-in deals with the EA's magic. Identify which ladder a deal belongs to by its order ticket against the recorded ticket sets (comment tag as a cross-check). If the armed timeout passes with no fill: remove all pendings, report `ABORT ARM_TIMEOUT`.
- **First entry deal -> TRIGGERED:** stamp the time. Never cancel the triggered ladder. Its remaining orders are filling on the same tick. Start the winner deadline.
- **CANCELLING:** async-remove every order of the losing ladder that is still pending. Track the outcome of each remove. A remove that fails because the order no longer exists means that order filled or is filling. If any losing-ladder entry deal is observed at any point before the burst, set `both_sided`, do not burst, remove everything remaining, and report `ABORT BOTH_SIDED`.
- **Winner completion:** when entry deals for the triggered ladder equal the expected count, the winner is complete. If the winner deadline passes with orders still unfilled (price bounced), remove them and `ABORT WINNER_SHORT`.
- **BURSTING:** in `AFTER_CANCEL` mode start only after the losing ladder's removes have all resolved cleanly. In `PARALLEL` mode start immediately on trigger. Fire the contingent burst (`CS` or `CB`) with async market orders using the same fill policy and invalid-stops retry the existing `OPEN` path uses. Wait for every request to resolve. If the count of successful opens is short after the existing retry passes, `ABORT BURST_SHORT`.
- **DONE:** report counts and timestamps. Python reconciles.
- **ABORTED:** remove every pending matching **this symbol + this machine's magic** (not just the recorded tickets), then report. Python decides about flattening positions.
- `OnInit`: delete any pendings carrying the EA magic, across all symbols. This is the **only** magic-wide sweep — the magic is shared, so every other sweep is symbol+magic scoped.
- If `OnInit` runs while a machine was mid-arm (EA restart), that machine's persisted state is gone; Python sees no `DONE`, times out, sweeps and restarts idle.

### Sweep scope (all symbol+magic, magic shared across symbols)

`ABORTARM`, `CANCELALL`, `ABORTED` cleanup, and every Python-side `cancel_all_pendings`
are scoped to (symbol, magic). A magic-only sweep would cancel another symbol's live
armed ladder and cause a `BOTH_SIDED`-class mess that looks like a strategy bug. Only
`OnInit` sweeps magic-wide.

MQL5 details to verify on the Weltrade demo and not assume: pending order fill policy (typically `ORDER_FILLING_RETURN`), whether the symbol allows `ORDER_TIME_SPECIFIED`, deal-history selection window for `HistoryDealSelect` inside `OnTradeTransaction`, and whether limit fills land at exactly the limit price.

Test-only inputs on the EA (default off, clearly named, logged at init when on):
- `TestCancelDelayMs`: delay the losing-ladder removes by N ms to widen the race window.
- `TestUnfillableWinner`: place one order of each ladder at a price deeper than the level so the winner never completes.

## 8. Python changes

Files expected to change (names from the repo, confirm by reading): `core/ea_bridge.py`, `core/bulk_orders.py`, `core/engine/queued_close_strategy_engine.py`, `core/persistence/repository.py`, `core/strategy_orchestrator.py`, `core/config_manager.py`, `core/ea_provisioner.py` (**EA install and compile step**), `api/server.py`, `static/index.html`, `experts/WTExecutor.mq5`, `mq5_run.md`.

**`main.py` is not in this list and is not changed.** Doc 08 previously said so; that was wrong. The EA is installed, compiled and pinged by `core/ea_provisioner.py::ensure_ea_ready` (called from the `api/server.py` startup hook), which copies the source when the SHA differs, recompiles when the `.ex5` is older than the source, and judges success by a fresh `.ex5` mtime — so a compile error already surfaces as `EAStatus(available=False, reason="EA compile failed")`. Phase 7 keeps the `WT_EA_VERSION` constant in `ea_provisioner.py` in lockstep with `#define WT_EA_VERSION` in the `.mq5`; the provisioner compares them and warns on a stale compiled EA.

`core/trading_engine.py` is not expected to change: the max-runtime timer stays engine-level and only calls `strategy.stop()`.

**Bridge:** add `arm_limit(plan) -> phased result`, `abort_arm(id)`, `cancel_all_pendings(symbol, magic)`. `arm_limit` sends the command, waits for the immediate ack, then polls that command's **per-symbol phase file non-destructively** with per-phase and overall deadlines; it must never hang and never delete the phase file between polls. The sweep is symbol+magic scoped, with a direct fallback used when the EA is unresponsive: `orders_get(symbol=mt5_symbol)` filtered by magic, then `TRADE_ACTION_REMOVE` per order.

**Plan builder (bulk_orders):** given config, current mid and offset, compute `lower = mid - offset_eff`, `upper = mid + offset_eff`, build both ladders and both contingent bursts, with TP/SL computed from each scenario's center using the existing slot formulas. Constant-side lines carry no TP/SL. Reuse the existing lot splitting.

Worked example, moving = BUY, mid 1000, offset 10:
- Lower scenario (center 990): `PB` buys at 990 with moving-slot TP/SL anchored on 990; `CS` constant sells at market, no TP/SL.
- Upper scenario (center 1010): `PS` constant sells at 1010, no TP/SL; `CB` moving buys at market with moving-slot TP/SL anchored on 1010.
- Note that which side carries TP/SL follows the moving/constant setting, not which side triggered.

**Target computation is a pure function of center.** `compute_constant_targets(...)` and the
moving TP/SL level helpers are called with each scenario's center at plan time (to fill in
the orders' TP/SL) and then called **again** after the terminal phase with the triggered
level. Only the post-trigger call is persisted. Center is **the limit level that triggered**,
never the average fill price, so the Python-side targets match the anchors the broker
actually used on the ladder orders.

**Engine open path:** add the `limit_trigger` branch next to the burst branch.
1. Preflight. 2. Persist `ARMED` (phase, levels, cmd id — **not** the pending ticket list; see persistence below). 3. `arm_limit`, waiting for `ARMED`. 4. Wait for a terminal phase. 5. Reconcile against `positions_get` and `orders_get`: per-side counts match config, no leftover pendings, tickets map to slots via tags. **`positions_get` can lag deal events, so retry the reconcile for ~1 s before declaring a mismatch.** 6. Set the cycle center to the triggered level, recompute and persist the constant targets for that center, and **call the same shared post-open routine the burst path uses.**

**There is no shared post-open routine today.** The burst path's post-open work is inlined
at the tail of `_try_bulk_open` (tag→ticket map into `moving_positions`/`constant_tickets`,
`save_moving_position`, `save_constant_ticket`, `log_fire`). Extract exactly that into one
routine and have both modes call it. Do not duplicate it. **Leave the sequential fallback
path in `_start_new_cycle_at_market` alone** unless it fits the extracted routine without a
rewrite.

**No sequential fallback for `limit_trigger`.** If the EA is unavailable, unhealthy, or
returns `UseSequentialFallback`, the mode refuses to arm and reports why. Burst mode keeps
its existing fallback untouched.
- `ABORT ARM_TIMEOUT`: re-arm around the current price (logged as a re-arm, not a new cycle).
- Any other `ABORT`, or reconcile failure: remove pendings, flatten, restart the cycle, increment the failure counter. At `max_consecutive_open_failures` stop the symbol and surface a loud error. A successful open resets the counter. **The counter lives in memory only** — it is not persisted, so a restart clears it, which is the intended behaviour.

**Persistence and recovery:** store `ARMED` in `symbol_state.metadata` (JSON) alongside the
existing strategy keys — phase, both levels, and the cmd id. **Do not store the pending
ticket list**: recovery sweeps by symbol+magic, so persisted tickets would be shadow state
that can go stale (agent/04's "no shadow state"). On startup reconcile: remove any pending
order matching this symbol+magic, then if the persisted phase was `ARMED` or mid-open,
flatten any positions from that open and restart idle. The DB is still deleted at session
start per existing behavior.

**Lifecycle hooks:** while `ARMED`, graceful stop sends `ABORTARM` and stops with no positions to wait on; terminate-symbol and terminate-all must sweep pendings (EA command first, direct fallback second) before and after the position close, including the "nuclear fallback" account scan; the graceful-stop hard-timeout path must do the same. All sweeps are symbol+magic scoped. Note the existing `_force_close_everything` already ends with an unconditional direct `_close_position` loop over survivors — that backstop is the natural place to hang the sweep so no path bypasses it.

**Persistence and recovery:** add the `ARMED` phase and pending ticket set to the repository. On startup reconcile: any pending order carrying our magic means remove it. If the persisted phase was `ARMED` or mid-open, flatten any positions from that open and restart idle. The DB is still deleted at session start per existing behavior.

**Logging and metrics:** use the activity logger for every phase (`[LIMIT]` prefix: preflight result, effective offset and whether clamped, levels, armed counts, trigger side and latency, cancel result, burst result, reconcile result, abort reason). After every successful open in **both modes**, append one row to `logs/users/{user}/sessions/open_quality.csv` with: timestamp, symbol, mode, trigger side (or n/a), per side: order count, distinct fill prices, min, max, modal price, modal share, first-to-last fill time span (ms), and from the EA: trigger-to-cancel-done and trigger-to-burst-done ms. This is how you compare modes on real data.

**UI:** per-symbol `Open Mode` dropdown and `Entry Offset` input in the existing asset panel, Bot State shows `Armed - waiting for trigger`, nothing else restyled.

## 9. Edge cases and required handling

| Case | Required behavior |
|---|---|
| Price gaps through both levels | Both ladders partly fill. `BOTH_SIDED` -> flatten and restart. |
| Winner only partially fills, price bounces | Deadline passes -> `WINNER_SHORT` -> remove, flatten, restart. |
| A remove fails with "order not found" | Treat as a possible losing-ladder fill. Check deals. If confirmed, `BOTH_SIDED`. |
| Neither level touched | After armed timeout, remove and re-arm around the current price. |
| Python crashes while armed | Pendings stay on the broker. On restart Python (and EA `OnInit`) removes them. Server-side expiry is the last-resort net. |
| EA/MT5 restarts while armed | `OnInit` sweeps pendings by magic. Python sees no `DONE`, times out, sweeps, restarts idle. |
| User presses Stop / Terminate while armed | `ABORTARM` or sweep, no positions to close, state goes idle. |
| Pending cap or volume limit too low | Preflight fails loudly, nothing is placed. |
| Offset below stops level | Clamp up, log loudly with both values. |
| Reconcile mismatch after `DONE` | Retry the reconcile for ~1 s (`positions_get` lags deal events). Only then treat as failure: flatten and restart, count it. |
| Repeated failures | Circuit breaker stops the symbol after N and says why. Counter is in-memory only. |
| Two symbols armed at once | Separate machines, separate phase files, separate request tables. `OPEN`/`CLOSE` from a third symbol may interleave through the shared command slot without touching either machine. |
| Pending cap reached by another symbol's armed ladder | Preflight counts **all** account pendings, so this fails loudly before anything is placed. |

## 10. Test plan (manual, on the demo, then on the VPS)

Anthony tests by watching MT5 live, so each test names exactly what to watch.

1. Preflight failure (set offset tiny, then a symbol with a low volume limit): nothing placed, clear error in log and UI.
2. Happy path, lower trigger: all buy limits at one price, sell limits gone, sells opened, counts right, cycle runs normally.
3. Happy path, upper trigger: mirror of 2.
4. Never touched: re-arm happens after the timeout, old pendings gone from MT5's orders tab.
5. Stop while armed. 6. Terminate-all while armed. Both: zero pendings and zero positions left.
7. Kill Python while armed, restart: pendings removed, state idle.
8. Close MT5 or reinit the EA while armed: pendings removed on init.
9. Race: run with `TestCancelDelayMs` set and the tightest offset on FX Vol 99 until `BOTH_SIDED` occurs. Expect flatten and restart, no stranded positions.
10. Winner short: `TestUnfillableWinner` on. Expect `WINNER_SHORT`, flatten, restart.
11. Circuit breaker: force repeated aborts, expect stop after N with a clear message.
12. Burst mode regression: run an ordinary burst cycle and confirm it behaves exactly as before.
13. Dispersion comparison: 30 cycles each of burst and limit_trigger on the VPS, read `open_quality.csv`.

## 11. Build phases

0. Read and map, no edits. 1. Config plumbing (default burst, zero behavior change). 2. EA: arm machines, place, ack, abort, init sweep, per-symbol phase files. 3. EA: trigger, cancel, burst, deadlines, test flags. 4. Python bridge and plan builder. 5. Engine integration, lifecycle hooks, persistence and recovery. 6. Metrics and UI. 7. Install pipeline (`core/ea_provisioner.py`) and `mq5_run.md`. 8. Final audit against doc 06 plus this doc's section 12.

### How closes are executed today (Phase 0 finding)

For reference when wiring the pendings sweep:

| Path | Mechanism | Entry point |
|---|---|---|
| Single position close (queue drain, cycle-end remainder, abort straggler) | direct synchronous `mt5.order_send`, `TRADE_ACTION_DEAL` with `position=` | `QueuedCloseStrategyEngine._close_position` |
| Bulk close, 3+ positions | EA batch via `bridge.close_tickets` (`T\|ticket` lines, `OrderSendAsync`) | `_ea_close_tickets` via `_force_close_everything` |
| Bulk-open abort cleanup | EA `CLOSEALL`, then direct `_close_position` for stragglers | `bulk_orders._abort_cleanup` |
| Terminate-all nuclear account scan | direct `mt5.order_send`, position-by-position, all symbols | `StrategyOrchestrator.terminate_all` |

`_force_close_everything` is a hybrid: EA batch first when `>= 3` positions, then an
unconditional direct `_close_position` loop over survivors. Direct `order_send` is the
backstop for every EA close path.

**Cross-symbol contention.** `EABridge._roundtrip` holds one `asyncio.Lock` for a whole
roundtrip, so symbol A's `close_tickets` already serialises against symbol B's `open_batch`
today. `OnTimer` returns early while `busy`, so a third symbol's command waits for the
in-flight burst. Both are pre-existing and out of scope — `ARMLIMIT` returning an immediate
ack and clearing `busy` means an armed machine holds no bridge lock and never occupies the
shared slot for the armed window.

## 12. Definition of done (additions to doc 06)

- Burst mode behaves identically to before (phase 8 regression run).
- No code path can leave a pending order behind: stop, terminate, terminate-all, timeout hard stop, crash restart, EA reinit all verified.
- Every EA phase ends in `DONE` or an `ABORT` with a reason, never a silent hang. Every Python wait has a deadline.
- No duplicated post-open logic between modes.
- No state mirrored in two places (see learnings: shadow state).
- Suffixed MT5 symbol name used consistently for every broker call and every `orders_get`. (Note: on this fork `mt5_symbol` returns `self.symbol` unsuffixed per agent/02 §2 — Weltrade has no suffix and no suffix logic exists anywhere. The requirement is that every new call goes through the `mt5_symbol` property, never `self.symbol`.)
- Failure path always ends flattened and restarted, or stopped by the breaker with a reason.
- `open_quality.csv` rows written for both modes.
- Test-only EA inputs default to off and log a warning at init when on.
- Armed machines never gate on, and are never gated by, the shared `busy` flag; `OPEN` and `CLOSE` are byte-for-byte unchanged.
- Every sweep is symbol+magic scoped except `OnInit`.
- No pending ticket list persisted anywhere.