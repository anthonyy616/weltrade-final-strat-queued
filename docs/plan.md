# MT5 Disconnect Recovery and Session Continuity Plan

**Status:** NOT STARTED — awaiting review and approval  
**Scope:** Server-side resilience and lifecycle recovery only; no strategy formula or trading-policy change.

## 1. Target outcome

When MT5 temporarily loses authorization/connectivity, the application must preserve the running session rather than silently becoming stopped or requiring the user to invoke `start-all` manually. If MT5 reconnects while the configured session window is still valid, the system should:

1. record the disconnect and recovery timestamps;
2. pause the session clock during the outage and persist the frozen `remaining_seconds` value;
3. pause trading safely while disconnected;
4. reconnect and reconcile broker positions, pending orders, and persisted strategy state before resuming ticks;
5. resume the existing cycle when ownership/state can be proven;
6. if state cannot be proven safe, cancel scoped pendings, close scoped positions, reset persistence safely, and automatically perform the equivalent of a controlled start/reconcile without user intervention;
7. stop cleanly only when the session deadline has elapsed or recovery has reached an explicit, observable safety terminal state.

The recovery path must be idempotent: repeated health failures, reconnect callbacks, or process restarts must not open duplicate pools, delete a live database, or issue conflicting cleanup commands.

## 2. Verified repository findings

### Existing behavior to preserve

- [TradingEngine](/Users/anthonyogbuah/Documents/codes-websites/weltrade-final-strat/core/trading_engine.py) polls MT5 health every 100 ticks and retries initialization up to 10 times with a 5-second delay.
- A successful reconnect calls `_reconcile_after_reconnect()` before normal tick processing resumes.
- [QueuedCloseStrategyEngine](/Users/anthonyogbuah/Documents/codes-websites/weltrade-final-strat/core/engine/queued_close_strategy_engine.py) already has an execution lock, `catching_up` gate, persisted phases (`ACTIVE`, `RESETTING`, `ARMED`), pending-order sweeping, state rebuild, and missed-closure replay.
- [Repository](/Users/anthonyogbuah/Documents/codes-websites/weltrade-final-strat/core/persistence/repository.py) persists strategy state in SQLite and scopes records by symbol.
- [StrategyOrchestrator](/Users/anthonyogbuah/Documents/codes-websites/weltrade-final-strat/core/strategy_orchestrator.py) provides graceful stop, terminate, and terminate-all operations.

### Gaps causing the reported failure mode

1. **Engine loss is terminal.** After the retry budget is exhausted, `run_tick_loop()` raises/logs the failure and its `finally` block sets `trading_engine.running = False`. There is no internal watchdog/recovery supervisor that restarts the engine while the session remains valid.
2. **No durable disconnect/session checkpoint.** `start_time`, timeout flags, and force-stop time exist only in memory. A process restart cannot determine when the outage began, whether the session deadline has passed, or whether the previous session was intentionally running.
3. **Reconnect reconciliation is too narrow.** It iterates only currently registered/running strategies. It does not define a symbol-level recovery result or a fallback when persisted state is missing, inconsistent, or unrecoverable.
4. **The start endpoint is destructive.** `/control/start` in [api/server.py](/Users/anthonyogbuah/Documents/codes-websites/weltrade-final-strat/api/server.py) calls terminate-all preparation and deletes `db/` before starting. This is appropriate only for an explicit fresh-session action, not for automatic recovery.
5. **Delayed cleanup is unsafe for continuity.** `_schedule_db_cleanup()` deletes the SQLite file five minutes after a graceful timeout stop. A delayed task can race with a restart/recovery and destroy the evidence needed to reconcile live positions.
6. **Database lock handling is too destructive.** `_release_db_locks()` can terminate/kill other processes holding the database. Automatic recovery must never kill an unrelated process or the active application process merely to make deletion succeed.
7. **Run-state persistence is disconnected from startup recovery.** [run_state.py](/Users/anthonyogbuah/Documents/codes-websites/weltrade-final-strat/core/run_state.py) records whether a user was running, but startup does not enumerate and restore those users. This is separate from MT5 reconnect and must be integrated or explicitly bounded.
8. **Multi-user ownership is under-specified.** The shared `db/grid_v3.db` and shared magic number require recovery to remain scoped by symbol and user/session ownership. A cleanup fallback must not flatten another active user’s positions.
9. **Health endpoint is process-only.** `/health` returns `ok` without MT5/recovery status, so an external monitor cannot distinguish a healthy trading engine from an HTTP server with a stopped engine.

## 3. Proposed recovery architecture

### 3.1 Explicit lifecycle state machine

Add a durable, observable application lifecycle state (global plus per symbol where needed):

- `RUNNING`: ticks and strategy processing are allowed.
- `DISCONNECTED`: MT5 health failed; no new trading actions; disconnect checkpoint written once.
- `RECONNECTING`: bounded retry/backoff is active.
- `RECONCILING`: MT5 is connected, but strategy ticks remain gated while broker/persistence state is compared.
- `RECOVERED`: reconciliation succeeded; return to `RUNNING`.
- `RECOVERY_RESETTING`: safe fallback is closing/sweeping and clearing only verified-owned state.
- `STOPPING`: session deadline or explicit stop is being completed.
- `STOPPED`: no trading work remains.
- `RECOVERY_BLOCKED`: safety could not be established; retain evidence and surface a clear alert rather than silently continuing.

Transitions must be serialized by one engine-level recovery lock and be safe if the same transition is requested twice.

### 3.2 Durable session/recovery checkpoint

Create a small versioned JSON/SQLite record (prefer the existing SQLite repository or a separate atomic JSON checkpoint if it must survive DB replacement) containing:

- session/run identifier and user/symbol scope;
- `session_started_at`, configured `max_runtime_minutes`, and computed `session_deadline_at`;
- `last_healthy_at`, `disconnect_started_at`, `reconnected_at`;
- lifecycle state and recovery attempt number;
- last successful reconciliation summary;
- whether the session was user-stopped, timed out, or eligible for automatic resume;
- schema/version and last update timestamp.

Writes must be atomic (temporary file + replace if JSON) and must not expose credentials. On startup, stale/incomplete checkpoints are treated as recovery input, not as permission to blindly start new trades.

### 3.3 Recovery decision policy

After MT5 reconnect or application restart:

1. Read the checkpoint and persisted per-symbol strategy state.
2. Compute remaining session time from the original deadline.
3. If explicitly stopped, graceful stop was already active, or the persisted remaining time is zero, do not open a new cycle; perform only the graceful-stop/safety path.
4. If time remains, instantiate/recover the configured bot/symbols, connect repositories, sweep only this bot’s pending orders, rebuild state, and reconcile positions/closures.
5. If reconciliation proves ownership and state consistency, resume the existing cycle without opening a new pool.
6. If a DB-owned pair ticket is gone from MT5, use the existing per-set `OFFLINE_RECOVERY` nuclear reset, limited to that symbol, set, and tickets owned by this session. Restart that set only when time remains and graceful stop is not active. If graceful stop is active, flatten the set and remain stopped.
7. If an orphan is live in MT5 but unknown to the DB, log and flag it only; never auto-close it.
8. If any close/cancel/verification step cannot be confirmed, enter `RECOVERY_BLOCKED`, keep the engine from opening new trades, emit a high-severity log/status event, and retain the DB/checkpoint for diagnosis.

The fallback must not use the public `/control/start` endpoint, because that endpoint currently means “fresh destructive start.” Extract a shared internal lifecycle service with separate `resume_existing_session()` and `start_fresh_session()` operations.

## 4. Phased implementation roadmap

### Phase 1 — Recovery contract and observability
**Status:** NOT STARTED  
**Objective:** Define state transitions and make the current failure visible without changing trading behavior.

**Tasks**
- Add typed lifecycle/recovery state structures and transition validation.
- Add structured recovery events with session id, symbol, phase, attempt, reason, and result; never log credentials.
- Extend status and `/health` with engine lifecycle, MT5 connectivity, disconnect duration, session deadline/remaining time, recovery phase, and last reconciliation result.
- Add a recovery lock and ensure only one recovery supervisor can run.

**Files/components:** `core/trading_engine.py`, new recovery module under `core/`, `api/server.py`, targeted tests.  
**Dependencies:** None.  
**Acceptance criteria:** Repeated health failures produce one durable transition sequence; `/health` reports degraded/recovering state instead of unconditional `ok`; normal healthy tick behavior is unchanged.  
**Verification:** Unit-test transitions/idempotency; run existing tests and a mocked MT5 health-failure sequence.

### Phase 2 — Durable session checkpoint and deadline preservation
**Status:** NOT STARTED  
**Objective:** Make disconnect/restart recovery decisions from persisted facts.

**Tasks**
- Add atomic checkpoint storage with schema version and migration behavior.
- Persist session start/deadline at the moment a fresh session starts; do not recompute it after reconnect.
- Persist disconnect start once, update retry/recovery metadata, and mark intentional stop/timeout distinctly.
- Replace in-memory-only timeout assumptions with checkpoint-aware deadline checks.
- Prevent delayed cleanup from deleting a checkpoint or active DB needed by a resumable session.

**Files/components:** `core/trading_engine.py`, `core/run_state.py` or new lifecycle persistence module, `core/persistence/repository.py`, `api/server.py`.  
**Dependencies:** Phase 1.  
**Acceptance criteria:** Restart during a simulated disconnect preserves the original deadline; a reconnect after the deadline never opens a new cycle; a still-valid session remains eligible for recovery.  
**Verification:** Temporary-directory persistence tests, clock-injected deadline tests, restart simulation.

### Phase 3 — Non-destructive reconnect supervisor
**Status:** NOT STARTED  
**Objective:** Keep the process alive and recover MT5 without manual `start-all`.

**Tasks**
- Refactor bounded reconnect attempts into a supervisor with backoff, cancellation, and a configurable total recovery policy.
- On health failure, pause/gate tick processing and checkpoint before reconnecting.
- On successful initialization, call reconciliation once through the recovery lock, then resume only after a complete result.
- Use a fixed recovery budget of **3 attempts per hour for 3 hours (9 attempts total)**. Do not tie retries to the session deadline.
- After the ninth attempt, persist `RECOVERY_FAILED` with the frozen remaining session time, stop retrying, and require a manual restart. A manual restart must enter the same reconcile path rather than blindly opening a fresh cycle.
- If the application exits after the budget is exhausted, preserve checkpoint state so the manual restart can resume reconciliation safely.

**Files/components:** `core/trading_engine.py`, MT5 adapter/helpers, configuration.  
**Dependencies:** Phases 1–2.  
**Acceptance criteria:** The log sequence in the report (authorization failures followed by successful reconnect) results in automatic continuation while time remains; no `CANCELALL` is emitted solely because of a transient reconnect; no duplicate cycle opens.  
**Verification:** Mock MT5 sequences: fail/fail/succeed, prolonged failure, reconnect during timeout, and repeated health failures.

### Phase 4 — Comprehensive broker/persistence reconciliation
**Status:** NOT STARTED  
**Objective:** Make recovery outcomes explicit and safe for every symbol.

**Tasks**
- Have `reconcile_on_startup()` return a typed result: resumed, per-set reset-required, blocked, orphan-flagged, or no-active-session, with evidence counts.
- Discover configured/running symbols and bot-owned terminal positions/orders even when an in-memory strategy is absent.
- Verify magic number, symbol, cycle id, tracked tickets, constant queue, armed phase, and unexpected/orphan positions.
- Preserve `catching_up`/execution-lock atomicity across the full replay.
- Make reconciliation retry transient MT5 visibility lag, then fail explicitly; never treat an exception as a successful empty state.
- Preserve the existing per-set `OFFLINE_RECOVERY` path when a DB-owned ticket is absent from MT5; scope it to the symbol/set/session-owned tickets only.
- Log and flag MT5 positions that are not represented in the DB; do not automatically close these orphans.
- Require owner/session match, running state, non-graceful-stop state, positive frozen remaining time, and a clean reconcile result before setting `resumed: true`.

**Files/components:** `core/engine/queued_close_strategy_engine.py`, `core/strategy_orchestrator.py`, `core/bot_manager.py`, `core/persistence/repository.py`.  
**Dependencies:** Phase 3.  
**Acceptance criteria:** A connected recovery either resumes a provably managed cycle or returns a non-success result that triggers the reset/blocked path; it never silently resumes with unknown exposure.  
**Verification:** Persistence fixtures for `ACTIVE`, `RESETTING`, `ARMED`, missing DB rows, orphan tickets, delayed `positions_get`, and already-closed moving tickets.

### Phase 5 — Automatic safe reset and internal session resumption
**Status:** NOT STARTED  
**Objective:** Implement the user-described “reconcile/recover as if start-all” behavior without destructive endpoint semantics.

**Tasks**
- Extract shared fresh-session preparation from `/control/start` into an internal service.
- Implement ordered reset: stop EA arm machines, cancel scoped pendings, close scoped positions, re-query until confirmed, then clear only verified session/symbol persistence.
- Do not delete `db/` while the application/repository is open; close repository connections through ownership-aware APIs first.
- Keep process termination and broad DB deletion available only behind explicit lifecycle gates: explicit Terminate All, confirmed timeout completion, or a fresh start when no resumable session exists.
- `_release_db_locks` may target only identified lock-holder PIDs, only after normal close/release attempts fail, and must log loudly before terminating or killing them. It must never run while a session is active, recovering, or in `RECOVERY_FAILED`.
- Start a new cycle only after zero scoped exposure and clean persistence are verified, and only if frozen remaining time is positive and graceful stop was not active.
- Keep explicit `/control/start` behavior documented as fresh/destructive, while automatic recovery uses the non-destructive internal path.

**Files/components:** `api/server.py`, `core/strategy_orchestrator.py`, `core/persistence/repository.py`, new lifecycle service.  
**Dependencies:** Phase 4.  
**Acceptance criteria:** A corrupted/missing cycle state is flattened and restarted automatically when safe; DB deletion failure does not leave live positions unmanaged or cause process killing; another user/symbol is not affected.  
**Verification:** Integration tests with mocked orders/positions and locked SQLite handles; idempotent repeated reset tests.

### Phase 6 — Startup-after-process-restart restoration
**Status:** NOT STARTED  
**Objective:** Recover valid sessions after the Python process itself restarts, not only after an in-process MT5 reconnect.

**Tasks**
- Integrate `run_state_manager` with startup discovery, or replace it with the new checkpoint while preserving multi-user scope.
- Restore only users/symbols whose checkpoint says they were running and whose session deadline remains valid.
- Ensure EA provisioning happens before recovery and that no strategy starts before MT5 connectivity is confirmed.
- Reconcile all restored symbols before enabling their tick processing.
- Mark stale intentional stops and expired sessions stopped, with safety verification only.

**Files/components:** `api/server.py`, `core/bot_manager.py`, `core/run_state.py`, lifecycle persistence.  
**Dependencies:** Phases 2, 4, and 5.  
**Acceptance criteria:** Process restart with live managed positions resumes them without duplicate opening; process restart after expiration does not resume; missing/corrupt checkpoint fails closed and is observable.  
**Verification:** Controlled restart tests and demo/VPS runbook.

### Phase 7 — Timeout/cleanup redesign and production validation
**Status:** NOT STARTED  
**Objective:** Remove cleanup races and validate the complete failure matrix.

**Tasks**
- Gate the existing five-minute cleanup timer: it may run only after confirmed timeout completion, zero owned positions/orders, closed repositories, and no resumable checkpoint. Skip it while positions are being reconciled or closed.
- Gate boot-time DB deletion and broad cleanup the same way; never delete recovery data during an active session, reconnect, reconciliation, or `RECOVERY_FAILED`.
- Add startup stale-resource diagnostics rather than automatic broad deletion when a resumable checkpoint exists.
- Add recovery metrics and structured summaries for reconnect duration, reset count, blocked count, and last safe broker verification.
- Add tests and a manual VPS soak scenario covering authorization failures, terminal restart, Python restart, DB lock, timeout crossing during outage, orphan pending orders, and repeated reconnects.
- Update README and operational documentation with recovery state meanings, logs, health endpoint, and manual escalation criteria.

**Files/components:** `core/trading_engine.py`, `api/server.py`, persistence, `README.md`, operational notes, tests.  
**Dependencies:** Phases 1–6.  
**Acceptance criteria:** All failure-matrix cases have a documented expected result and automated/mock coverage where possible; the VPS runbook demonstrates recovery without manual start-all for a transient outage; blocked cases are explicit and safe.  
**Verification:** Targeted test suite, static checks, and approved demo-account soak test.

## 5. Important decisions requiring approval

1. **Recovery budget — APPROVED:** Attempt 3 times per hour for 3 hours, 9 attempts total. On exhaustion persist `RECOVERY_FAILED`, freeze remaining time, stop retrying, and require a manual restart through reconciliation.
2. **Unknown-state policy — APPROVED:** A DB-owned missing ticket uses scoped per-set `OFFLINE_RECOVERY`; an MT5 orphan unknown to the DB is logged/flagged and never auto-closed. Restart only with remaining time and no graceful stop.
3. **DB deletion/process-kill policy — APPROVED WITH GATES:** Preserve the existing capabilities, but permit them only for explicit Terminate All, confirmed timeout completion, or a fresh start with no resumable session. PID termination requires failed normal release attempts, exact lock-holder identification, and loud logging. Never run during active/recovering/`RECOVERY_FAILED` sessions.
4. **Multi-user isolation — APPROVED MINIMUM:** Treat the VPS/process as single-owner. Persist `user_id` and `session_id` in run state and DB rows. Resume only when the owner matches. Reconcile only DB-owned tickets with the expected magic number. Per-user magic numbers are a follow-up, not part of this change.
5. **Session clock — APPROVED:** Pause the timer during disconnect/recovery. Persist `remaining_seconds` and a heartbeat every few seconds; resume from the last checkpoint after a crash. If remaining time is zero or graceful stop was already active, reconcile and go directly to graceful stop.
6. **Hard-stop failsafe — APPROVED:** Persist its remaining duration and pause it during an outage. Do not run DB cleanup until positions are confirmed closed.
7. **MT5 terminal authorization:** Repeated `(-6, 'Terminal: Authorization failed')` is a connectivity/authentication outage, not a strategy failure. The system retries within the fixed budget but cannot invent credentials or repair broker configuration; exhaustion becomes `RECOVERY_FAILED` and remains visible.

## 6. Manual setup and external dependencies

- A demo MT5 terminal/account and approved VPS test window are required for live recovery validation.
- Confirm the terminal remains running and the configured account/server credentials are valid; the application must not log or persist the password.
- Provide an approved way to simulate terminal disconnect/restart and, separately, a Python process restart.
- Confirm whether the deployment supervisor restarts the process after an unrecoverable crash; if so, preserve and test the checkpoint handoff.
- Review and approve the orphan/unknown-state flattening policy before enabling it on any non-demo account.

## 7. Definition of done

- A transient MT5 disconnect followed by successful reconnect resumes the existing session automatically when the saved state says running, the owner matches, graceful stop is inactive, remaining time is positive, and reconciliation is clean.
- Session timing is paused during disconnects and continuous across process restarts through persisted `remaining_seconds` and heartbeat checkpoints.
- Exactly nine reconnect attempts are allowed: three per hour over three hours; exhaustion persists `RECOVERY_FAILED` and requires manual restart through reconciliation.
- Reconciliation runs before ticks/trades resume and produces an explicit result.
- No duplicate pool/cycle is opened during repeated reconnect callbacks.
- Automatic recovery never deletes `db/` or kills lock holders while active, recovering, or `RECOVERY_FAILED`; gated cleanup is allowed only in the approved terminal cases.
- Missing DB-owned tickets trigger only symbol/set-scoped `OFFLINE_RECOVERY`; unknown MT5 orphans are logged and flagged, never auto-closed.
- Unsafe/ambiguous states fail closed, flatten only verified session-owned exposure, or remain visibly blocked.
- Expired, graceful-stopped, or zero-remaining sessions do not restart a set.
- `/health` and `/status` expose enough recovery state for a monitor/operator to distinguish healthy, reconnecting, recovered, stopped, and blocked states.
- Targeted automated tests pass, existing tests/regressions pass, and the approved VPS/demo soak test is completed.
- Related documentation and operational escalation steps are updated.

## 8. Recommended next action

Review the decisions in section 5, especially the maximum outage policy, ownership isolation, and unknown-state flattening rule. After approval, implement phases sequentially, beginning with the lifecycle contract and observability; do not change trading strategy formulas or open/close rules as part of the recovery work.
