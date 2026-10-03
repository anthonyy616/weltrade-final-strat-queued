# Design Decisions & Spec Deviations

Every place the implementation had to make a call that isn't literally spelled out in `agent/01`–`06`. Nothing here contradicts a hard rule; these are the judgment calls, all additive, all documented so the next reader doesn't mistake them for drift.

## Confirmed with Anthony before the build

### D1 — Raw price units instead of `* point`
**Spec:** agent/02 §6 formulas end with `* point`; agent/03 says "reuse the existing `point` property pattern."
**Reality:** the existing `GridBounceStrategyEngine` adds pip values as raw price units (`tp = entry + tp_pips` — no point multiplication anywhere), and the live config's values (`grid_distance: 25`, `tp_pips: 30` on FX Vol 20) only make sense on that scale. Multiplying by point (0.001 for these indices) would have made every distance 1000× smaller than the working system.
**Decision (Anthony):** use raw price units. The formulas are otherwise implemented exactly; the worked-example verification passes at `point=1`, which is consistent with this reading.
**Consequence:** the engine has no `point` property. If a future broker/symbol genuinely needs pip-scaling, that's an explicit new decision — don't add it silently.

### D2 — DB deletion on boot kept; reconciliation scope narrowed accordingly
**Spec:** agent/07 Phase 7 requires hard-kill + restart recovery, which implies the DB survives restarts.
**Reality:** `api/server.py` deletes `db/grid_v3.db` on boot and on `/control/start` (fresh-session behavior), and there's a 5-min post-session DB cleanup in the trading engine.
**Decision (Anthony):** keep the deletion behavior.
**Consequence:** reconciliation covers (a) MT5 connection-loss reconnects mid-session — the primary race the docs worry about — and (b) process restarts that don't trigger the boot-time deletion path. A **Start All click wipes recovery state by design**. If a hard-kill recovery test is run, restart the process without pressing Start All (the orchestrator's `reconcile_strategies_on_startup` runs on bot restore), or temporarily disable the boot-time delete.
**Flagged for testing:** this is the most likely place to hit an apparent "reconciliation doesn't work" result that is actually the fresh-session delete doing its job.

### D3 — One-pass implementation, manual testing afterwards
**Spec:** agent/07 gates each phase on live MT5 verification.
**Reality:** MetaTrader5 Python package is Windows-only; the dev environment is macOS. No MT5 connection possible from here.
**Decision (Anthony):** implement everything now; he runs Phase 10 manually.

## Judgment calls made during implementation (additive, documented)

### J1 — Config snapshot fields added to `QueuedCloseState`
The spec's state dataclass doesn't include `moving_side`, `moving_freq`, `constant_freq`, `grid_distance` — but agent/05's "config values must be snapshotted" rule requires them to live *somewhere* on state. Added as explicit state fields, captured once in `start()`, persisted in `symbol_state.metadata` for recovery. This is the structural fix the docs demand, not shadow state: config is never read alongside them.

### J2 — Lot sizes on state (`moving_lot`, `constant_lot`)
Same reasoning as J1: the pool-opening loop and PnL math need lots after startup; re-reading config mid-cycle would violate the snapshot rule. Persisted in metadata so reconciliation restores them.

### J3 — `MovingPositionRecord.lot`
Spec's dataclass has no `lot`; PnL computation per closure needs it. Additive field with a default, doesn't affect the persistence schema (which already had no lot column — lot is uniform per side from config).

### J4 — TP/SL identification uses deal history first, proximity second
The old engine latched "touch flags" then inferred TP-vs-SL by which level price was nearer. This engine calls `_extract_close_info` (deal history, OUT deal, reason code) first — exact, and it's the same function reconciliation uses. Proximity inference against the last tick is the fallback when history returns nothing (e.g. deal-history window not covering the close). If you observe a wrong TP/SL classification in logs, check whether history was available before suspecting the queue logic.

### J5 — `TERMINATE_QUEUE_EXHAUSTED` as a third cycle-complete reason
agent/02 §11 names `NORMAL` and `EARLY_FORCE_CLOSE`. agent/02 §8's terminate-all-on-last-failure restarts the cycle, which flows through the same `_end_cycle` path. Logged with its own reason rather than forcing it into either of the other two — it's a genuinely different event. `log_cycle_complete` accepts all three.

### J6 — Empty-queue/ticket mismatch guard
If the queue has pending entries but `constant_tickets` is empty (can only happen through an external manual close of a constant position), the queue is cleared with an error log rather than spinning or force-terminating. Not in the edge-case table; chosen as the least destructive interpretation of "the strategy state is inconsistent." **If this fires during testing, it means a constant position was closed outside the bot — flag it, don't ignore it.**

### J7 — `volatility_tolerance` retained in the Pydantic `GlobalConfig` only
The concept doesn't exist in this strategy (it fed the Grid-Bounce volatility reset). Removed from `ConfigManager` defaults and the UI, but the API model still accepts it so an old cached client posting a config payload doesn't get a 422. It's stored if sent, never read by logic. Remove the field from `GlobalConfig` once all clients are on the new UI.

### J8 — Partial pool on order failure
agent/03 says "handle partial failures" at startup without specifying recovery. Implementation: a failed order logs an error and the loop continues (pool opens partially). The cycle still ends by count semantics — but note `moving_total`/`constant_total` still reflect **configured** totals, so a partially-opened pool will hit its cycle-end via the force-close path (e.g. all 3 of 3 opened moving positions close → cycle ends even though constants never opened). A fully-failed constant side means the queue drains the "no constant tickets" guard (J6). **Testing note:** if you see cycle-end immediately after startup, count what actually opened in MT5 before suspecting the counts.

### J9 — `updateLotInputs` kept as a no-op
Several surviving call sites referenced it (checkbox change handlers, copy flows). Rather than chasing every reference in a 1600-line file, the function remains as a documented no-op. Cosmetic-only.

### J10 — Old engine file left in place
`core/engine/grid_bounce_strategy_engine.py` is now unreferenced by any import (verified by grep). Left in the repo rather than deleted — it's the reference for the MT5 interaction patterns and Anthony may want it for comparison. Safe to delete whenever.

## Rules explicitly honored (checked, not assumed)

- `catching_up` check inside the lock, reconciliation holds it across the whole pass (agent/05 atomicity section).
- One `execution_lock` block per tick covering detection → queue → cycle-end (agent/05 concrete mistake).
- Snapshot iteration of `close_queue` (agent/05).
- One retry attempt per item per tick (agent/03 retry rule).
- Two separate target lists, no shared index space, no sign tricks (agent/04).
- Running counts, no slot-binding of constant tickets (agent/04).
- No `if reversed:` branch — verified by automated string check (agent/03 step 5).
- No live-config reads of the three snapshot values in the tick path — verified by automated string check (agent/05).
- `mt5_symbol` property used in every broker call (agent/04).
- Constant-side order requests never carry `sl`/`tp` (agent/04, comment at call site).
- 1-based slot indices everywhere: formulas, DB rows, logs (agent/05 petty list).
- Cycle-end explicitly deletes `constant_queue` rows for the ended cycle (agent/04 persistence rule).
- Terminate clears queue + counts + DB rows in the same operation (agent/05 terminate-during-drain section).
- Reconciliation replays through `_handle_moving_closure` — one closure path (agent/03).
- OUT-deal filtering in deal history (agent/05).
