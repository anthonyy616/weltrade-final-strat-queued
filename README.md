# Weltrade queued-close bot

## EA bulk executor (WTExecutor)

The bot can open and close position batches through the `WTExecutor.mq5` EA
instead of one-by-one sequential MT5 calls (sequential path remains the
fallback when the EA is unavailable).

**No manual EA setup is required.** On first run the bot:

1. copies `experts/WTExecutor.mq5` into the terminal's `MQL5\Experts` folder,
2. compiles it with MetaEditor automatically,
3. pings the EA to confirm it responds (version check).

MT5 may restart once during this — this only happens when no positions are
open. Set `EA_AUTO_RESTART=false` in `.env` to disable the automatic restart.

Diagnostics on the Windows machine:

```bash
python tools/ea_doctor.py             # paths, install, compile, ping
python tools/ea_doctor.py --relaunch  # also exercise the MT5 restart path
python tools/ea_doctor.py --smoke     # open 5+5 demo orders, verify comments, close (DEMO ONLY)
```

Lot/volume limits come from `MAX_CONFIGS.json` in the repo root
(`MAX_LOT_PER_ASSET` per-order split cap, `MAX_VOLUME_PER_ASSET` per-symbol
total cap, `MIN_STOP_PIPS_PER_ASSET`).

`mq5_run.md` describes the old manual AsyncBurstProbe test procedure and is
kept for reference only.
