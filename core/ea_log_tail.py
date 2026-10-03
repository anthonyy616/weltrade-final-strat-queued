"""Background tail of the EA's log file (plan phase D).

Reads new bytes of <common>/Files/wt_ea.log from a saved offset every 0.5 s
and re-emits each line through logging.getLogger("ea") so EA output shows in
the VS Code terminal and logs/bot.log.
"""

import asyncio
import logging

logger = logging.getLogger("ea")

POLL_S = 0.5
EA_LOG_NAME = "wt_ea.log"


class EALogTail:
    def __init__(self, bridge):
        self.bridge = bridge
        self._task: asyncio.Task | None = None
        self._offset = None   # start at end of file on boot

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self):
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"EA log tail poll failed: {e}")
            await asyncio.sleep(POLL_S)

    async def _poll_once(self):
        # Bridge resolves the common folder lazily; if MT5 isn't up yet just wait.
        try:
            self.bridge._paths()
        except Exception:
            return
        path = self.bridge.log_file
        if path is None or not path.exists():
            return
        size = path.stat().st_size
        if self._offset is None:
            self._offset = size   # start at end of file on boot
        if size < self._offset:
            self._offset = 0      # file shrank (rotated) — start over
        if size == self._offset:
            return
        with path.open("rb") as f:
            f.seek(self._offset)
            data = f.read()
        self._offset = size
        text = data.decode("ascii", errors="replace")
        for line in text.splitlines():
            line = line.rstrip("\r\n")
            if line:
                logger.info("[EA] " + line)
