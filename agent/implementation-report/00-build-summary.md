# Queued Close Strategy — Build Summary

**Branch:** `test` (all work committed here; `main` untouched)
**Scope:** Full replacement of the Grid Bounce strategy with the Queued Close Strategy, implemented across Phases 2–9 of `agent/07-implementation-plan-and-agent-prompts.md`.

## Commit history (in order)

| Commit | Content |
|---|---|
| `c361576` | Phase 2 — Config schema rewrite (`ConfigManager`, Pydantic model) |
| `9786a18` | Phase 3 — State model, new DB tables, pure formula functions (verified against the spec's worked example) |
| `739ae95` | Phases 4–7 — The engine: startup pool, tick handling, queue/retry, cycle-end, persistence wiring, reconnect reconciliation |
| `f5d358e` | Phase 8 — Orchestrator, trading-engine reconnect reconciliation hook, API wiring |
| `9d60de3` | Phase 9 — UI panel replacement, moving/constant status readout |

## Files touched

| File | Change |
|---|---|
| `core/config_manager.py` | Rewritten for the new per-symbol schema |
| `api/server.py` | `SymbolConfig`/`GlobalConfig` Pydantic models replaced |
| `core/engine/queued_close_strategy_engine.py` | **New file** — dataclasses, pure formulas, engine |
| `core/persistence/repository.py` | 4 new tables + CRUD methods |
| `core/engine/activity_logger.py` | New `LEG_NAMES`, `log_cycle_complete()` |
| `core/strategy_orchestrator.py` | Swapped to `QueuedCloseStrategyEngine`, aggregate status fields |
| `core/trading_engine.py` | `_reconcile_after_reconnect()` wired into `_reconnect_mt5()` |
| `static/index.html` | Per-symbol panel, status tiles, config payload, copy feature |

Files **not** touched: `core/bot_manager.py`, `core/session_logger.py`, `main.py`, `api/server.py` route shapes (only models changed).

## Decisions made during the build (confirmed with Anthony before starting)

1. **Pip conversion — raw price units.** The architecture doc's formulas say `n * moving_freq * point`, but the existing working engine adds pip values directly (`tp = entry + tp_pips`, no point multiplication) and the live config uses values on that scale. Per Anthony's decision, the new engine uses **raw price units** — `grid_level_up + n * moving_freq`. The `point` property pattern is therefore not needed, but `mt5_symbol` is still routed through the property per agent/04.
2. **DB deletion on boot kept.** `api/server.py` deletes `db/grid_v3.db` on boot and on `/control/start` ("fresh session"). This coexists with reconciliation as follows: reconciliation covers **mid-session MT5 reconnects** and process restarts that don't go through the fresh-session path. Pressing Start All wipes recovery state by design.
3. **Cadence — everything in one pass.** All phases implemented without live MT5 checkpoints; Anthony tests manually afterwards (Phase 10 is his soak test).

## What was verified before handoff

- All Python files compile (`py_compile` clean across the full project).
- UI JavaScript extracted and syntax-checked with `node --check`.
- Formula functions verified programmatically against the exact worked example in agent/02 §6 for both moving=BUY and moving=SELL (all 8 number sequences match).
- Automated assertions confirmed: no `if reversed:`-style branch anywhere in the engine; no live-config reads of `moving_freq`/`constant_freq`/`grid_distance` in the tick path.
- No stale references to `GridBounceStrategyEngine` remain outside its own (now-unreferenced) file. The old engine file was left in place, unreferenced.

## What is NOT verified (requires MT5 demo terminal — Anthony's Phase 10)

Everything in `agent/06-definition-of-done-checklist.md` that requires watching MT5: real pool opens, TP/SL prices in the Trade tab, constant positions having no stops, forced-closure queue behavior, reversal behavior, asymmetric force-close, hard-kill reconciliation, retry/terminate-on-last-item. See `05-test-plan.md` for the step-by-step.
