import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger("ea")

# Phase file written by the EA for each armed command (doc 08 section 6).
# The name carries the command id, so a stale file from an earlier cycle can
# never be mistaken for the current one.
PHASE_FILE_PREFIX = "wt_arm_"
PHASE_FILE_EXT = ".txt"

# Interim phases (the EA may report several before settling). DONE and ABORT
# are terminal.
TERMINAL_PHASES = ("DONE", "ABORT")


class EABridgeError(RuntimeError):
    pass


class EABridge:
    def __init__(self):
        # Resolved lazily on first use (plan phase B): the terminal may not be
        # running yet at import time, and the folder can change after a relaunch.
        self.dir = None            # type: Path | None
        self.cmd = None            # type: Path | None
        self.tmp = None            # type: Path | None
        self.res = None            # type: Path | None
        self.log_file = None       # type: Path | None  #EA-side wt_ea.log
        self._lock = asyncio.Lock()   # the EA handles one command at a time

    def resolve_common_files(self) -> Path:
        """Resolve the MT5 Common\\Files folder: env MT5_COMMON_FILES ->
        mt5.terminal_info().commondata_path + \\Files -> APPDATA default."""
        env = os.getenv("MT5_COMMON_FILES")
        if env:
            return Path(os.path.expandvars(env))
        try:
            import MetaTrader5 as mt5
            ti = mt5.terminal_info()
            if ti is not None and getattr(ti, "commondata_path", None):
                return Path(ti.commondata_path) / "Files"
        except Exception:
            pass
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files"
        raise EABridgeError(
            "Cannot resolve MT5 Common\\Files folder — set MT5_COMMON_FILES")

    def invalidate(self):
        """Forget the resolved folder (call after any MT5 relaunch so the next
        command re-resolves, per plan phase B item 1)."""
        self.dir = None
        self.cmd = self.tmp = self.res = self.log_file = None

    def _paths(self):
        if self.dir is None:
            self.dir = self.resolve_common_files()
            self.cmd = self.dir / "wt_cmd.txt"
            self.tmp = self.dir / "wt_cmd.tmp"
            self.res = self.dir / "wt_res.txt"
            self.log_file = self.dir / "wt_ea.log"
            logger.info(f"EA bridge using common files folder: {self.dir}")

    async def _roundtrip(self, header, body, timeout_s=15.0, rid=None):
        self._paths()
        async with self._lock:
            if rid is None:
                rid = uuid.uuid4().hex[:8]
            self.res.unlink(missing_ok=True)
            text = "\n".join([f"id={rid}", *header, *body]) + "\n"
            self.tmp.write_text(text, encoding="ascii")
            os.replace(self.tmp, self.cmd)
            end = time.monotonic() + timeout_s
            while time.monotonic() < end:
                if self.res.exists():
                    try:
                        parsed = self._parse(self.res.read_text(encoding="ascii"))
                    except OSError:
                        parsed = {}
                    if parsed.get("id") == rid:
                        return parsed
                await asyncio.sleep(0.005)
            # Never leave a stale command behind: if the EA wakes up later it
            # would otherwise fire an old OPEN batch.
            self.cmd.unlink(missing_ok=True)
            raise EABridgeError("EA did not answer; is it attached with Algo Trading on?")

    @staticmethod
    def _parse(raw):
        out = {"fails": [], "pending": []}
        for line in raw.splitlines():
            if line.startswith("F|"):
                _, tag, code = line.split("|")
                out["fails"].append((tag, int(code)))
            elif line.startswith("P|"):
                out["pending"].append(line[2:])
            elif "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out

    async def ping(self, timeout_s: float = 3.0) -> str:
        """Ping the EA; returns its reported version string (plan phase B)."""
        r = await self._roundtrip(["action=PING"], [], timeout_s=timeout_s)
        return r.get("version", "unknown")

    async def healthy(self, timeout_s: float = 1.0) -> bool:
        """True if the EA answers a ping; never raises (plan phase B)."""
        try:
            await self.ping(timeout_s=timeout_s)
            return True
        except Exception:
            return False

    async def open_batch(self, symbol, magic, orders):
        """orders: list of {'side': 'B'|'S', 'lot': float, 'tp': float, 'sl': float, 'tag': str}.
        Tags must be unique and at most 31 characters. tp/sl of 0 means no broker stop."""
        body = [f"O|{o['side']}|{o['lot']:.2f}|{o.get('tp', 0.0):.5f}|"
                f"{o.get('sl', 0.0):.5f}|{o['tag']}" for o in orders]
        return await self._roundtrip(
            ["action=OPEN", f"symbol={symbol}", f"magic={magic}"], body)

    async def close_tickets(self, symbol, magic, tickets):
        return await self._roundtrip(
            ["action=CLOSE", f"symbol={symbol}", f"magic={magic}"],
            [f"T|{t}" for t in tickets])

    async def close_all(self, symbol, magic):
        return await self._roundtrip(
            ["action=CLOSEALL", f"symbol={symbol}", f"magic={magic}"], [])

    # ------------------------------------------------------------------
    # Limit-trigger arm protocol (doc 08 sections 6/8)
    #
    # arm_limit() sends the command, waits for the IMMEDIATE ack, then polls
    # this command's own phase file WITHOUT deleting it. The bridge lock is
    # released before the polling loop starts, so an armed symbol never holds
    # the bridge and never blocks another symbol's OPEN/CLOSE.
    # ------------------------------------------------------------------

    def phase_file_path(self, cmd_id):
        self._paths()
        return self.dir / f"{PHASE_FILE_PREFIX}{cmd_id}{PHASE_FILE_EXT}"

    @staticmethod
    def _read_phase(path):
        """Read a phase file non-destructively. Returns None when it does not
        exist yet or cannot be parsed -- the EA may be mid-write."""
        try:
            raw = path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            return None
        out = {}
        for line in raw.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k] = v
        return out if "phase" in out else None

    async def arm_limit(self, symbol, magic, plan, *,
                        armed_timeout_ms=120000,
                        win_fill_deadline_ms=1500,
                        cancel_ack_deadline_ms=1500,
                        on_phase=None,
                        overall_timeout_s=None,
                        armed_slack_s=5.0,
                        cmd_id=None):
        """Arm a limit-trigger ladder and wait for a terminal phase.

        `plan` is the dict from bulk_orders.build_limit_plan(). Returns the
        terminal phase dict (phase=DONE or phase=ABORT with `reason`).

        Every wait has a deadline: one for the ARMED phase and one overall.
        Nothing here can hang. on_phase, if given, is called with each interim
        phase dict as it arrives.
        """
        self._paths()
        # Callers may pass their own id so the ARMED phase can be persisted
        # with the command id BEFORE the arm starts (doc 08 section 8).
        rid = cmd_id or uuid.uuid4().hex[:8]

        body = [
            f"{ln['role']}|{ln['slot']}|{ln['lot']:.2f}|"
            f"{ln['tp']:.5f}|{ln['sl']:.5f}|{ln['tag']}"
            for ln in plan["lines"]
        ]
        header = [
            "action=ARMLIMIT",
            f"symbol={symbol}",
            f"magic={magic}",
            f"lower={plan['lower']:.5f}",
            f"upper={plan['upper']:.5f}",
            f"armed_timeout_ms={int(armed_timeout_ms)}",
            f"win_fill_deadline_ms={int(win_fill_deadline_ms)}",
            f"cancel_ack_deadline_ms={int(cancel_ack_deadline_ms)}",
        ]

        # The ack is quick; the armed window is not, so the ack gets its own
        # short timeout and the bridge lock is released before we start polling.
        ack = await self._roundtrip(header, body, timeout_s=10.0, rid=rid)
        if ack.get("accepted") != "1":
            raise EABridgeError(
                f"EA refused to arm {symbol}: {ack.get('reason', 'unknown')}")

        phase_path = self.phase_file_path(rid)
        armed_deadline = time.monotonic() + (
            armed_timeout_ms / 1000.0) + armed_slack_s
        overall = (overall_timeout_s if overall_timeout_s is not None else
                   (armed_timeout_ms + win_fill_deadline_ms
                    + cancel_ack_deadline_ms) / 1000.0 + 10.0)
        overall_deadline = time.monotonic() + overall

        last = None
        last_phase = None
        while True:
            data = self._read_phase(phase_path)
            if data and data.get("phase") != last_phase:
                last_phase = data["phase"]
                last = data
                logger.info(
                    f"[LIMIT] {symbol} cmd={rid} phase={data['phase']}"
                    + (f" reason={data.get('reason')}"
                       if data.get("reason") else ""))
                if on_phase is not None:
                    on_phase(data)
                if data["phase"] in TERMINAL_PHASES:
                    return data

            now = time.monotonic()
            if now >= overall_deadline:
                raise EABridgeError(
                    f"arm_limit on {symbol} (cmd {rid}) exceeded its "
                    f"{overall:.1f}s overall deadline; last phase="
                    f"{last_phase or 'none'}")
            if now >= armed_deadline and last_phase in (None, "PLACING"):
                raise EABridgeError(
                    f"arm_limit on {symbol} (cmd {rid}) never reported ARMED "
                    f"within {armed_timeout_ms / 1000.0 + armed_slack_s:.1f}s; "
                    f"last phase={last_phase or 'none'}")
            await asyncio.sleep(0.01)

    async def abort_arm(self, symbol, magic, cmd_id=None):
        """Cancel everything armed for this symbol. If the EA does not answer,
        fall back to sweeping the pendings directly -- a stop or terminate must
        never leave a pending order behind."""
        try:
            res = await self._roundtrip(
                ["action=ABORTARM", f"symbol={symbol}", f"magic={magic}"], [],
                timeout_s=5.0)
            logger.info(
                f"[LIMIT] abort_arm {symbol}: accepted={res.get('accepted')} "
                f"reason={res.get('reason')}")
            return res
        except Exception as e:
            logger.error(
                f"abort_arm via EA failed for {symbol} ({e}) -- "
                "falling back to a direct pending sweep")
            return await self.cancel_all_pendings(symbol, magic)

    async def cancel_all_pendings(self, symbol, magic, deadline_s=10.0):
        """Remove every pending order for (symbol, magic).

        Asks the EA first, then verifies with orders_get and removes whatever
        is left with TRADE_ACTION_REMOVE. Scoped to symbol+magic because the
        magic is shared across symbols -- a magic-wide sweep would cancel
        another symbol's live ladder.

        Uses `symbol` directly because callers pass the already-resolved
        broker symbol (the engine's mt5_symbol property).
        """
        import MetaTrader5 as mt5

        end = time.monotonic() + deadline_s
        try:
            await self._roundtrip(
                ["action=CANCELALL", f"symbol={symbol}", f"magic={magic}"], [],
                timeout_s=3.0)
        except Exception as e:
            logger.warning(f"CANCELALL via EA failed for {symbol} ({e})")

        while True:
            def _pending():
                orders = mt5.orders_get(symbol=symbol) or ()
                return [o.ticket for o in orders if o.magic == magic]

            pending = await asyncio.to_thread(_pending)
            if not pending:
                logger.info(f"[LIMIT] pending sweep clean: {symbol} magic={magic}")
                return True
            if time.monotonic() >= end:
                logger.error(
                    f"[LIMIT] pending sweep TIMED OUT for {symbol}: "
                    f"{len(pending)} pending order(s) remain: {pending}")
                return False

            def _remove(tickets):
                removed = []
                for tkt in tickets:
                    try:
                        res = mt5.order_send({
                            "action": mt5.TRADE_ACTION_REMOVE,
                            "symbol": symbol,
                            "position": tkt,
                            "magic": magic,
                            "comment": "py-sweep",
                        })
                    except Exception as e:
                        logger.error(f"order_send remove failed for {tkt}: {e}")
                        continue
                    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
                        removed.append(tkt)
                    else:
                        logger.error(
                            f"remove {tkt} on {symbol} rejected: "
                            f"{res.comment if res else mt5.last_error()}")
                return removed

            removed = await asyncio.to_thread(_remove, pending)
            logger.info(
                f"[LIMIT] direct pending sweep removed {len(removed)}/"
                f"{len(pending)} on {symbol}")
            await asyncio.sleep(0.2)

    def clear_limit_phase_files(self):
        """Remove persisted arm phase files after an explicit global reset."""
        self._paths()
        removed = 0
        for path in self.dir.glob(f"{PHASE_FILE_PREFIX}*{PHASE_FILE_EXT}"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed