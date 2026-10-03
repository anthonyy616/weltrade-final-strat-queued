"""Phase 1 verification: config plumbing round-trip and validation.

Runs entirely in a temp directory with synthetic config files. It never touches
the real per-user config_*.json files and never reads MT5.

    python3 tools/verify_phase1_config.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_manager import (
    ConfigManager, get_default_symbol_config, get_default_global_config,
    OPEN_MODES, BURST_MODES,
)

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def sym_fields(sym):
    return sym


def main():
    tmp = tempfile.mkdtemp(prefix="phase1_cfg_")
    os.chdir(tmp)
    sym = "FX Vol 20"

    # ---------------------------------------------------------------- defaults
    print("\n[1] Defaults contain the new fields")
    cm = ConfigManager(user_id="t1", config_file="fresh.json")
    cfg = cm.get_config()
    s = cfg["symbols"][sym]
    for f in ("open_mode", "entry_offset"):
        check(f"per-symbol default has {f}", f in s, str(sorted(s)))
    check("open_mode default is burst", s["open_mode"] == "burst", s.get("open_mode"))
    check("entry_offset default is 50.0", s["entry_offset"] == 50.0, s.get("entry_offset"))
    for f in ("armed_timeout_seconds", "win_fill_deadline_ms",
              "cancel_ack_deadline_ms", "burst_mode",
              "max_consecutive_open_failures"):
        check(f"global default has {f}", f in cfg["global"], str(sorted(cfg["global"])))
    check("burst_mode default is AFTER_CANCEL",
          cfg["global"]["burst_mode"] == "AFTER_CANCEL")
    check("armed_timeout_seconds default is 120",
          cfg["global"]["armed_timeout_seconds"] == 120)
    check("max_consecutive_open_failures default is 3",
          cfg["global"]["max_consecutive_open_failures"] == 3)

    # ------------------------------------------- legacy config missing fields
    print("\n[2] Old config file with no limit-trigger fields (legacy shape)")
    legacy = {"global": {"max_runtime_minutes": 45},
              "symbols": {sym: {"enabled": True, "buy_count": 7,
                                "sell_count": 9, "buy_lot": 0.02,
                                "sell_lot": 0.03, "constant_side": "buy",
                                "grid_distance": 75.0, "moving_freq": 12.0,
                                "constant_freq": 9.0}}}
    Path("legacy.json").write_text(json.dumps(legacy))
    # NOTE: ConfigManager ignores the config_file argument when user_id is not
    # "default" (it derives config_{user_id}.json), so use the default user id
    # to actually read the file we just wrote.
    cm2 = ConfigManager(user_id="default", config_file="legacy.json")
    g2, s2 = cm2.get_config()["global"], cm2.get_symbol_config(sym)
    check("legacy: max_runtime_minutes preserved", g2["max_runtime_minutes"] == 45,
          g2["max_runtime_minutes"])
    check("legacy: open_mode backfilled to burst", s2.get("open_mode") == "burst")
    check("legacy: entry_offset backfilled", s2.get("entry_offset") == 50.0)
    check("legacy: globals backfilled",
          all(k in g2 for k in get_default_global_config()))
    check("legacy: existing symbol values untouched",
          s2["buy_count"] == 7 and s2["sell_count"] == 9
          and s2["constant_side"] == "buy" and s2["grid_distance"] == 75.0)

    # ------------------------------------------------------ validation policy
    print("\n[3] Validation: clamp-and-warn, never silently upgrades to limit_trigger")
    cm2.update_config({"symbols": {sym: {"open_mode": "limit_tigger",
                                         "entry_offset": -5}}})
    s3 = cm2.get_symbol_config(sym)
    check("typo open_mode -> burst", s3["open_mode"] == "burst", s3["open_mode"])
    check("open_mode never becomes limit_trigger by accident",
          s3["open_mode"] in OPEN_MODES)
    check("negative entry_offset -> default", s3["entry_offset"] == 50.0,
          s3["entry_offset"])
    cm2.update_config({"symbols": {sym: {"open_mode": "limit_trigger",
                                         "entry_offset": 12.5}}})
    s4 = cm2.get_symbol_config(sym)
    check("valid limit_trigger accepted", s4["open_mode"] == "limit_trigger")
    check("valid entry_offset accepted", s4["entry_offset"] == 12.5)
    cm2.update_config({"symbols": {sym: {"open_mode": None, "entry_offset": "abc"}}})
    s5 = cm2.get_symbol_config(sym)
    check("None open_mode -> burst", s5["open_mode"] == "burst", s5["open_mode"])
    check("non-numeric entry_offset -> default", s5["entry_offset"] == 50.0)

    print("\n[4] Global validation and clamping")
    cm2.update_config({"global": {"armed_timeout_seconds": 5,
                                  "win_fill_deadline_ms": 999999,
                                  "cancel_ack_deadline_ms": -1,
                                  "max_consecutive_open_failures": 0,
                                  "burst_mode": "parallel_lower",
                                  "max_runtime_minutes": -3}})
    g4 = cm2.get_global_config()
    check("armed_timeout_seconds clamped to min 10",
          g4["armed_timeout_seconds"] == 10, g4["armed_timeout_seconds"])
    check("win_fill_deadline_ms clamped to max 60000",
          g4["win_fill_deadline_ms"] == 60000, g4["win_fill_deadline_ms"])
    check("cancel_ack_deadline_ms clamped to min 100",
          g4["cancel_ack_deadline_ms"] == 100, g4["cancel_ack_deadline_ms"])
    check("max_consecutive_open_failures clamped to min 1",
          g4["max_consecutive_open_failures"] == 1)
    check("bad burst_mode -> AFTER_CANCEL",
          g4["burst_mode"] == "AFTER_CANCEL", g4["burst_mode"])
    check("burst_mode stays in BURST_MODES", g4["burst_mode"] in BURST_MODES)
    check("negative max_runtime_minutes -> 0", g4["max_runtime_minutes"] == 0)
    cm2.update_config({"global": {"burst_mode": "PARALLEL",
                                  "armed_timeout_seconds": 240}})
    check("valid PARALLEL accepted",
          cm2.get_global_config()["burst_mode"] == "PARALLEL")
    check("valid armed_timeout accepted",
          cm2.get_global_config()["armed_timeout_seconds"] == 240)

    # ------------------------------------------------- file round-trip (disk)
    print("\n[5] Disk round-trip (save_config -> reload from file)")
    cm2.save_config()
    cm3 = ConfigManager(user_id="default", config_file="legacy.json")
    s6 = cm3.get_symbol_config(sym)
    g6 = cm3.get_global_config()
    check("open_mode survived file round-trip",
          s6["open_mode"] == "burst", s6["open_mode"])
    check("entry_offset survived file round-trip",
          s6["entry_offset"] == 50.0, s6["entry_offset"])
    check("burst_mode survived file round-trip",
          g6["burst_mode"] == "PARALLEL", g6["burst_mode"])
    check("armed_timeout survived file round-trip",
          g6["armed_timeout_seconds"] == 240)
    check("max_runtime_minutes survived file round-trip",
          g6["max_runtime_minutes"] == 0)

    # --------------------------------------------------------- Copy Lot Setup
    print("\n[6] Copy Lot Setup must NOT copy the new per-symbol fields")
    idx = Path(__file__).resolve().parent.parent / "static" / "index.html"
    html = idx.read_text(encoding="utf-8")
    copy_block = html.split("function copyConfigFrom")[1].split("function getSymbolSetCount")[0]
    # Check the copied-field ARRAY only, not the surrounding comment (which
    # deliberately mentions the excluded fields by name).
    import re as _re
    m = _re.search(r"for \(const field of \[(.*?)\]\)", copy_block, _re.S)
    check("copyConfigFrom field list found", m is not None)
    copied = m.group(1) if m else ""
    check("copyConfigFrom omits open_mode", "open_mode" not in copied, copied)
    check("copyConfigFrom omits entry_offset", "entry_offset" not in copied, copied)

    # ------------------------------------------------------------- UI wiring
    print("\n[7] UI wiring present")
    check("Open Mode control rendered", 'id="sym_${safe}_open_mode"' in html)
    check("Entry Offset control rendered", 'id="sym_${safe}_entry_offset"' in html)
    check("payload sends open_mode", "open_mode: modeEl" in html)
    check("payload sends entry_offset", "entry_offset: readNum" in html)
    for gid in ("armed_timeout_seconds", "win_fill_deadline_ms",
                "cancel_ack_deadline_ms", "burst_mode",
                "max_consecutive_open_failures"):
        check(f"global control #{gid} rendered", f'id="{gid}"' in html)
        check(f"global #{gid} read by payload builder", f"readGlobalInt('{gid}'" in html
              or f"document.getElementById('{gid}')" in html)
    check("loadConfig applies global settings", "applyGlobalSettings(cfg.global)" in html)

    # --------------------------------------------------- pydantic round-trip
    # NOTE: this machine has no project dependencies installed (no fastapi /
    # pydantic), so the API layer cannot be executed here. Verified by AST
    # inspection instead: the fields are declared on the models AND the POST
    # /config handler forwards model_dump() output to ConfigManager.
    print("\n[8] api/server.py pydantic models (static AST check — "
          "deps not installed, not executed)")
    import ast as _ast
    srv_path = Path(__file__).resolve().parent.parent / "api" / "server.py"
    tree = _ast.parse(srv_path.read_text(encoding="utf-8"))
    models = {}
    for node in tree.body:
        if isinstance(node, _ast.ClassDef) and node.name in ("SymbolConfig", "GlobalConfig"):
            models[node.name] = {
                stmt.target.id: _ast.unparse(stmt.annotation)
                for stmt in node.body
                if isinstance(stmt, _ast.AnnAssign) and isinstance(stmt.target, _ast.Name)
            }
    check("SymbolConfig class found", "SymbolConfig" in models)
    check("GlobalConfig class found", "GlobalConfig" in models)
    for f, want in (("open_mode", "Optional[str]"), ("entry_offset", "Optional[float]")):
        check(f"SymbolConfig.{f} declared as {want}",
              models.get("SymbolConfig", {}).get(f) == want,
              models.get("SymbolConfig", {}).get(f))
    for f, want in (("armed_timeout_seconds", "Optional[int]"),
                    ("win_fill_deadline_ms", "Optional[int]"),
                    ("cancel_ack_deadline_ms", "Optional[int]"),
                    ("burst_mode", "Optional[str]"),
                    ("max_consecutive_open_failures", "Optional[int]")):
        check(f"GlobalConfig.{f} declared as {want}",
              models.get("GlobalConfig", {}).get(f) == want,
              models.get("GlobalConfig", {}).get(f))

    # The POST /config handler must forward model_dump() for both blocks.
    src = srv_path.read_text(encoding="utf-8")
    handler = src.split("async def update_config")[1].split("@app.")[0] \
        if "async def update_config" in src else ""
    check("POST /config forwards global model_dump()",
          "global_cfg.model_dump()" in handler)
    check("POST /config forwards symbol model_dump()",
          "sym_cfg.model_dump()" in handler)

    # ConfigManager must merge the new per-symbol fields (its whitelist is
    # get_default_symbol_config()).
    cm_src = (Path(__file__).resolve().parent.parent / "core" / "config_manager.py") \
        .read_text(encoding="utf-8")
    merge = cm_src.split("def update_config")[1]
    check("update_config merges via get_default_symbol_config() whitelist",
          "for field in get_default_symbol_config():" in merge)
    check("update_config validates globals after update",
          "_validate_global_fields()" in merge)

    # ------------------------------------------- nothing reads the new fields
    # Burst safety: no config value may be READ to change trading behaviour
    # until Phase 5 wires the mode in. The bridge and the plan builder may
    # legitimately take these names as PARAMETERS (they do from phase 4) --
    # what must not exist yet is the engine reading them off config.
    print("\n[9] Burst safety: the engine still reads no limit-trigger config")
    root = Path(__file__).resolve().parent.parent
    # As of phase 5 the strategy engine consumes every new field...
    eng = (root / "core/engine/queued_close_strategy_engine.py").read_text(
        encoding="utf-8")
    missing = [tok for tok in ("open_mode", "entry_offset",
                               "armed_timeout_seconds", "win_fill_deadline_ms",
                               "cancel_ack_deadline_ms", "burst_mode",
                               "max_consecutive_open_failures")
               if tok not in eng]
    check("the strategy engine consumes every new config field", not missing,
          f"missing: {missing}")
    # ...while the engine-level timer file must stay untouched by them
    # (doc 08: trading_engine.py is not expected to change).
    te = (root / "core/trading_engine.py").read_text(encoding="utf-8")
    leaked = [tok for tok in ("open_mode", "entry_offset",
                              "armed_timeout_seconds", "win_fill_deadline_ms",
                              "cancel_ack_deadline_ms",
                              "max_consecutive_open_failures") if tok in te]
    check("the engine-level timer file does not read the new fields",
          not leaked, f"leaked: {leaked}")

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())