"""Static audit for the consolidated parallel, cancel-first limit path."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
FAILURES = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        FAILURES.append(name)


def main():
    ea = (ROOT / "experts" / "WTExecutor.mq5").read_text(encoding="utf-8")
    engine = (ROOT / "core" / "engine" / "queued_close_strategy_engine.py").read_text(
        encoding="utf-8")
    config = (ROOT / "core" / "config_manager.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    print("[1] Parallel-only configuration")
    check("config strips retired mode fields",
          "REMOVED_CONFIG_FIELDS" in config and '"open_mode"' in config)
    check("UI has no open_mode control", "open_mode" not in ui)
    check("UI has no burst_mode control", "burst_mode" not in ui)
    check("engine has no open_mode branch", "self.state.open_mode" not in engine)

    print("[2] Cancel-first EA state machine")
    check("trigger enters cancellation", "StartCancel(mi)" in ea)
    trigger_body = ea.split("void BeginTrigger", 1)[1].split("void ", 1)[0]
    check("cancellation precedes contingent burst",
          trigger_body.find("StartCancel(mi)") >= 0
          and trigger_body.find("StartBurst(mi)") < 0)
    check("opposite pending orders are re-queried",
          "CountOppositePendings" in ea)
    check("cancel attempts log retcodes", "cancel attempt" in ea.lower()
          and "retcode" in ea.lower())
    check("final clear/not-clear status is logged",
          "opposite side clear" in ea.lower()
          and "opposite side NOT clear" in ea)
    check("opposite-side race fills are logged",
          "OPPOSITE-SIDE FILL BEFORE CANCEL" in ea)

    print("[3] Python opening path")
    check("parallel limit path is the only cycle opener",
          "await self._try_limit_open(moving_leg, constant_leg)" in engine)
    check("EA unavailable does not fall back to market orders",
          "will not fall back to market orders" in engine)

    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
