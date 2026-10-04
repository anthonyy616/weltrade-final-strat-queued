"""Verify parallel-only configuration plumbing without touching user config."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_manager import ConfigManager, get_default_global_config

FAILURES = []


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def main():
    with tempfile.TemporaryDirectory(prefix="phase1_cfg_") as tmp:
        os.chdir(tmp)
        symbol = "FX Vol 20"
        legacy = {
            "global": {"burst_mode": "PARALLEL", "armed_timeout_seconds": 240},
            "symbols": {symbol: {"open_mode": "burst", "entry_offset": 12.5}},
        }
        Path("config_default.json").write_text(json.dumps(legacy), encoding="utf-8")
        cm = ConfigManager(user_id="default", config_file="config_default.json")
        cfg = cm.get_config()
        sym = cfg["symbols"][symbol]
        glob = cfg["global"]

        check("parallel-only symbol config strips open_mode", "open_mode" not in sym)
        check("entry_offset remains configurable", sym.get("entry_offset") == 12.5)
        check("parallel-only global config strips burst_mode", "burst_mode" not in glob)
        check("timing defaults remain present",
              all(k in glob for k in ("armed_timeout_seconds",
                                      "win_fill_deadline_ms",
                                      "cancel_ack_deadline_ms")))
        check("failure breaker default remains present",
              glob.get("max_consecutive_open_failures") == 3)
        check("legacy values do not reappear after save/reload",
              "open_mode" not in sym and "burst_mode" not in glob)

        root = Path(__file__).resolve().parent.parent
        api = (root / "api" / "server.py").read_text(encoding="utf-8")
        ui = (root / "static" / "index.html").read_text(encoding="utf-8")
        defaults = get_default_global_config()
        check("API retains entry_offset", "entry_offset:" in api)
        check("API retains limit timing fields",
              all(f"{name}:" in api for name in (
                  "armed_timeout_seconds", "win_fill_deadline_ms",
                  "cancel_ack_deadline_ms")))
        check("UI has no legacy mode control", "open_mode" not in ui)
        check("UI has no legacy burst control", "burst_mode" not in ui)
        check("global defaults have no legacy burst mode", "burst_mode" not in defaults)

    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
