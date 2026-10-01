import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger("ea")


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
        self.log_file = None       # type: Path | None  (EA-side wt_ea.log)
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

    async def _roundtrip(self, header, body, timeout_s=15.0):
        self._paths()
        async with self._lock:
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