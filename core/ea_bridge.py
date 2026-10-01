import asyncio
import os
import time
import uuid
from pathlib import Path


class EABridgeError(RuntimeError):
    pass


class EABridge:
    def __init__(self):
        base = os.getenv("MT5_COMMON_FILES") or os.path.join(
            os.environ["APPDATA"], "MetaQuotes", "Terminal", "Common", "Files")
        self.dir = Path(base)
        self.cmd = self.dir / "wt_cmd.txt"
        self.tmp = self.dir / "wt_cmd.tmp"
        self.res = self.dir / "wt_res.txt"
        self._lock = asyncio.Lock()   # the EA handles one command at a time

    async def _roundtrip(self, header, body, timeout_s=15.0):
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

    async def ping(self):
        await self._roundtrip(["action=PING"], [], timeout_s=3.0)
        return True

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