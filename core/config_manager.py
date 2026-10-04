import json
import math
import os
from typing import Dict, Any, List, Optional

# All available trading symbols
AVAILABLE_SYMBOLS = [
    # FX Indices
    "FX Vol 20", "FX Vol 40", "FX Vol 60", "FX Vol 80", "FX Vol 99",
    # SFX Indices
    "SFX Vol 20", "SFX Vol 40", "SFX Vol 60", "SFX Vol 80", "SFX Vol 99",
    # FlipX Indices
    "FlipX 1", "FlipX 2", "FlipX 3", "FlipX 4", "FlipX 5",
    # PainX Indices
    "PainX 400", "PainX 600", "PainX 800", "PainX 999", "PainX 1200",
    # GainX Indices
    "GainX 400", "GainX 600", "GainX 800", "GainX 999", "GainX 1200",
    # Other Indices
    "SwitchX 600", "SwitchX 1200", "SwitchX 1800", "BreakX 1200", "BreakX 1800"
]

# Sane UI-level cap to prevent fat-fingered input from opening an absurd pool
MAX_POSITION_COUNT = 500

CONSTANT_SIDES = ("buy", "sell")

# Ranges for the global limit-trigger settings (doc 08 §4).
ARMED_TIMEOUT_RANGE = (10, 3600)
MS_DEADLINE_RANGE = (100, 60000)
MAX_FAILURES_RANGE = (1, 100)

# Old Grid Bounce fields, removed entirely for this fork (agent/02 architecture §3)
REMOVED_CONFIG_FIELDS = [
    "tp_pips", "sl_pips",
    "second_entry_buy_tp_pips", "second_entry_buy_sl_pips",
    "second_entry_sell_tp_pips", "second_entry_sell_sl_pips",
    "pair_buy_lot", "pair_sell_lot", "single_lot",
    "center_buy_lot", "center_sell_lot",
    "pair_buy_lots", "pair_sell_lots", "single_lots",
    "max_positions", "sets", "sets_config",
]


def get_default_symbol_config() -> Dict[str, Any]:
    return {
        "enabled": False,
        "buy_count": 5,
        "sell_count": 5,
        "buy_lot": 0.01,
        "sell_lot": 0.01,
        "constant_side": "sell",
        "grid_distance": 50.0,
        "moving_freq": 10.0,
        "constant_freq": 8.0,
        # entry_offset is in price units, the same unit as grid_distance; the
        # stops/spread floor is applied at arm time, not here.
        "entry_offset": 50.0,
    }


def get_default_global_config() -> Dict[str, Any]:
    """Global defaults. max_runtime_minutes is the existing engine timer; the
    rest are the limit-trigger knobs from doc 08 §4."""
    return {
        "max_runtime_minutes": 0,
        "armed_timeout_seconds": 120,
        "win_fill_deadline_ms": 1500,
        "cancel_ack_deadline_ms": 1500,
        "max_consecutive_open_failures": 3,
    }


class ConfigManager:
    """
    Multi-Asset Configuration Manager (Queued Close Strategy)

    Structure:
    {
        "global": {
            "max_runtime_minutes": 0
        },
        "symbols": {
            "FX Vol 20": { ...symbol config... },
            ...
        }
    }
    """

    def __init__(self, user_id: str = "default", config_file: str = "config.json"):
        self.user_id = user_id

        # If a specific user is logged in, use their unique config file
        if user_id and user_id != "default":
            self.config_file = f"config_{user_id}.json"
        else:
            self.config_file = config_file

        self.config: Dict[str, Any] = {}
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    loaded = json.load(f)

                symbols_data = loaded.get("symbols")
                is_new_format = isinstance(symbols_data, dict) and "global" in loaded

                if is_new_format:
                    self.config = loaded
                else:
                    # Old format from the Grid Bounce fork: rebuild from defaults,
                    # carrying over only global settings.
                    print("[CONFIG] Old-format config detected — rebuilding for Queued Close Strategy...")
                    self.config = self._get_defaults()
                    if isinstance(loaded, dict) and "max_runtime_minutes" in loaded:
                        self.config["global"]["max_runtime_minutes"] = loaded["max_runtime_minutes"]
                    self.save_config()

            except Exception as e:
                print(f"[CONFIG] Error loading config {self.config_file}: {e}")
                self.config = self._get_defaults()
        else:
            print(f"[CONFIG] Creating new config file: {self.config_file}")
            self.config = self._get_defaults()
            self.save_config()

        self._normalize_config()

    def _normalize_config(self):
        """Ensure every symbol entry has the full new schema and no removed fields."""
        # Globals first: a config file written before the limit-trigger fields
        # existed must still normalise to a complete global block.
        self._validate_global_fields()
        for symbol in list(self.config.get("symbols", {}).keys()):
            sym = self.config["symbols"][symbol]
            # Strip retired fields entirely. In particular, old open_mode
            # values must not select a different opening behavior.
            for field in REMOVED_CONFIG_FIELDS + ["open_mode"]:
                sym.pop(field, None)
            # Fill any missing new fields with defaults
            for field, default in get_default_symbol_config().items():
                if field not in sym:
                    sym[field] = default
            self._validate_symbol_fields(symbol)

    def _validate_global_fields(self) -> List[str]:
        """Validate/clamp the global settings in place. Returns list of warnings.

        Uses the same clamp-and-warn policy as the per-symbol fields (doc 08 §4):
        a bad value is corrected and reported, never silently accepted.
        """
        if not isinstance(self.config.get("global"), dict):
            self.config["global"] = get_default_global_config()
            return []

        warnings: List[str] = []
        defaults = get_default_global_config()
        gbl = self.config["global"]
        # Retire the old mode switch while accepting existing config files.
        gbl.pop("burst_mode", None)

        # Fill any key missing from an older config file with its default.
        for field, default in defaults.items():
            if field not in gbl:
                gbl[field] = default

        # max_runtime_minutes: 0 means "no timeout" — keep 0 valid.
        try:
            val = int(float(gbl.get("max_runtime_minutes", 0)))
            gbl["max_runtime_minutes"] = max(0, val)
        except (TypeError, ValueError):
            warnings.append(
                f"invalid max_runtime_minutes, reset to {defaults['max_runtime_minutes']}")
            gbl["max_runtime_minutes"] = defaults["max_runtime_minutes"]

        # Integer-with-range helper shared by the three numeric limit settings.
        def _int_in_range(field: str, rng: tuple) -> None:
            try:
                val = int(float(gbl.get(field)))
            except (TypeError, ValueError):
                warnings.append(
                    f"invalid {field}, reset to default {defaults[field]}")
                gbl[field] = defaults[field]
                return
            lo, hi = rng
            if val < lo:
                warnings.append(f"{field}={val} below minimum {lo}, clamped")
                val = lo
            elif val > hi:
                warnings.append(f"{field}={val} above maximum {hi}, clamped")
                val = hi
            gbl[field] = val

        _int_in_range("armed_timeout_seconds", ARMED_TIMEOUT_RANGE)
        _int_in_range("win_fill_deadline_ms", MS_DEADLINE_RANGE)
        _int_in_range("cancel_ack_deadline_ms", MS_DEADLINE_RANGE)
        _int_in_range("max_consecutive_open_failures", MAX_FAILURES_RANGE)

        for w in warnings:
            print(f"[CONFIG] global: {w}")
        return warnings

    def _validate_symbol_fields(self, symbol: str) -> List[str]:
        """Validate/clamp all new fields in place. Returns list of warnings."""
        sym = self.config["symbols"][symbol]
        warnings: List[str] = []

        # constant_side: strict whitelist, no silent fallback to default
        side = sym.get("constant_side")
        if side not in CONSTANT_SIDES:
            warnings.append(
                f"invalid constant_side '{side}' (must be 'buy' or 'sell')"
            )
            sym["constant_side"] = "sell"

        # Counts: positive integers within the UI-level cap
        for field in ("buy_count", "sell_count"):
            try:
                val = int(float(sym.get(field, 5)))
                if val < 1:
                    warnings.append(f"{field}={val} below minimum, clamped to 1")
                    val = 1
                if val > MAX_POSITION_COUNT:
                    warnings.append(f"{field}={val} above cap {MAX_POSITION_COUNT}, clamped")
                    val = MAX_POSITION_COUNT
                sym[field] = val
            except (TypeError, ValueError):
                warnings.append(f"invalid {field}, reset to default 5")
                sym[field] = 5

        # Lots: positive floats, floor 0.01
        for field in ("buy_lot", "sell_lot"):
            try:
                val = float(sym.get(field, 0.01))
                sym[field] = max(0.01, val)
            except (TypeError, ValueError):
                warnings.append(f"invalid {field}, reset to default 0.01")
                sym[field] = 0.01

        # Pips fields: positive floats
        for field in ("grid_distance", "moving_freq", "constant_freq"):
            try:
                val = float(sym.get(field, 0.0))
                if val <= 0:
                    warnings.append(f"{field}={val} must be > 0, reset to default")
                    val = get_default_symbol_config()[field]
                sym[field] = val
            except (TypeError, ValueError):
                warnings.append(f"invalid {field}, reset to default")
                sym[field] = get_default_symbol_config()[field]

        # constant_freq must be strictly less than moving_freq (baked into the
        # closing-target math, agent/02 §3). Policy choice: clamp, and log it.
        if sym["constant_freq"] >= sym["moving_freq"]:
            warnings.append(
                f"constant_freq ({sym['constant_freq']}) >= moving_freq "
                f"({sym['moving_freq']}) — clamped constant_freq to "
                f"moving_freq - 1.0"
            )
            sym["constant_freq"] = sym["moving_freq"] - 1.0
            if sym["constant_freq"] <= 0:
                sym["constant_freq"] = get_default_symbol_config()["constant_freq"]

        # entry_offset: must be a usable positive, finite price distance. The
        # stops/spread floor is applied at arm time regardless of what is
        # stored here.
        try:
            offset = float(sym.get("entry_offset"))
        except (TypeError, ValueError):
            offset = None
        if offset is None or not math.isfinite(offset) or offset <= 0:
            warnings.append(
                f"invalid entry_offset {sym.get('entry_offset')!r}, reset to "
                f"default {get_default_symbol_config()['entry_offset']}"
            )
            sym["entry_offset"] = get_default_symbol_config()["entry_offset"]
        else:
            sym["entry_offset"] = offset

        for w in warnings:
            print(f"[CONFIG] {symbol}: {w}")
        return warnings

    def save_config(self):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f" Error saving config: {e}")

    def update_config(self, new_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update config with new values.
        Handles both flat updates and nested symbol updates.
        """
        if "global" in new_config:
            global_updates = dict(new_config["global"])
            self.config["global"].update(global_updates)
            self._validate_global_fields()

        if "symbols" in new_config:
            for symbol, sym_cfg in new_config["symbols"].items():
                if symbol not in self.config["symbols"]:
                    continue
                # Merge only known new-schema fields; ignore removed/foreign keys
                for field in get_default_symbol_config():
                    if field in sym_cfg:
                        self.config["symbols"][symbol][field] = sym_cfg[field]
                # 'enabled' is toggled separately but may come in the same payload
                if "enabled" in sym_cfg:
                    self.config["symbols"][symbol]["enabled"] = bool(sym_cfg["enabled"])
                self._validate_symbol_fields(symbol)

        self.save_config()
        return self.config

    def get_config(self) -> Dict[str, Any]:
        return self.config

    def get_global_config(self) -> Dict[str, Any]:
        """Get global settings"""
        return self.config.get("global", {})

    def get_symbol_config(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get config for a specific symbol"""
        return self.config.get("symbols", {}).get(symbol)

    def get_enabled_symbols(self) -> List[str]:
        """Get list of symbols that are enabled"""
        enabled = []
        for symbol, cfg in self.config.get("symbols", {}).items():
            if cfg.get("enabled", False):
                enabled.append(symbol)
        return enabled

    def enable_symbol(self, symbol: str, enabled: bool = True):
        """Enable or disable a symbol"""
        if symbol in self.config.get("symbols", {}):
            self.config["symbols"][symbol]["enabled"] = enabled
            self.save_config()

    def _get_defaults(self) -> Dict[str, Any]:
        """Generate default multi-asset config structure"""
        return {
            "global": get_default_global_config(),
            "symbols": {
                symbol: get_default_symbol_config()
                for symbol in AVAILABLE_SYMBOLS
            }
        }
