# Limit-cycle stop behavior

## User-facing summary

The bot normally starts another cycle after a successful cycle until the configured session time ends. It can stop earlier when:

- the user requests a graceful or immediate stop;
- the session time limit is reached;
- repeated limit-trigger attempts fail and the configured failure limit is reached;
- the arm operation reaches its bounded re-arm limit without a trigger;
- the application, VPS, MT5 terminal, EA, or broker connection becomes unavailable;
- safety cleanup cannot confirm that the trading state is safe.

## Technical behavior

- A successful limit-trigger arm returns `DONE`. The engine reconciles the expected positions, registers them, and continues the normal cycle loop.
- A genuine `ABORT` with `ARM_TIMEOUT` is treated as a bounded re-arm, not as an immediate consecutive-open failure. The engine may re-arm up to `MAX_LIMIT_REARMS` times.
- Exhausting those re-arms becomes one definitive open failure. The configured `max_consecutive_open_failures` circuit breaker then determines whether the symbol restarts or stops.
- Other terminal EA abort reasons, preflight failures, reconciliation mismatches, unavailable/stale EA states, and bridge errors enter the failure path. That path removes scoped pending orders, closes positions when necessary, clears cycle state, and either starts a new cycle or stops at the breaker limit.
- A session time limit is enforced by the session loop. The UI can request a graceful stop when the time limit is reached and there are no active positions; active work is allowed to finish according to the stop policy.
- The EA writes a mutable per-command phase file plus a separate durable `DONE`/`ABORT` receipt. The Python bridge prioritizes the terminal receipt during polling and performs a final receipt read before declaring an overall timeout.
- Phase receipts are command-specific and are cleared during an explicit global phase reset. This prevents a prior command's terminal result from being attributed to a new command.

The safety behavior is intentionally conservative: if the system cannot establish whether orders or positions are still managed, it cleans up rather than silently continuing with an unknown state.
