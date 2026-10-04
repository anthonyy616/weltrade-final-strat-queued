"""EA provisioner: make sure the WTExecutor EA is installed, compiled and
attached before trading starts (plan phase C).

Never raises out of startup — on any failure it returns an unavailable status
and the bot falls back to the existing sequential opening path.
"""

import asyncio
import hashlib
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5

logger = logging.getLogger("ea")

# Repo source of truth for the EA (plan section 2: experts/WTExecutor.mq5)
EA_SOURCE = Path(__file__).resolve().parent.parent / "experts" / "WTExecutor.mq5"
# Must match #define WT_EA_VERSION in the .mq5 source (plan C.4)
WT_EA_VERSION = "1.5"

COMPILE_TIMEOUT = float(os.getenv("EA_COMPILE_TIMEOUT", "60"))
RELAUNCH_WAIT_S = 90.0
PING_WAIT_S = 30.0

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class EAStatus:
    available: bool
    version: Optional[str] = None
    reason: str = ""
    # True when an EA answered but is older than the repo source. It is
    # surfaced so the server can require recompilation before trading.
    stale: bool = False


def _decode_log(data: bytes) -> str:
    """MetaEditor logs are commonly UTF-16; try utf-16 then utf-8 (plan §4.1)."""
    for enc in ("utf-16", "utf-8"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("ascii", errors="replace")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compile_errors(log_text: str, limit: int = 6) -> str:
    """Pull the compiler's actual errors out of the MetaEditor log.

    A compile failure that only says "see the log above" is useless to whoever
    is reading the UI, and this log is the only record of why the EA is
    missing. Returns a compact one-line summary, or "" when there is nothing
    that looks like an error line.
    """
    errors = []
    for line in log_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # MetaEditor marks them like "path.mq5(123:4) : error 123: message"
        low = line.lower()
        if " : error" in low or low.startswith("error") or "fatal" in low:
            errors.append(line)
    if not errors:
        return ""
    # Keep the tail: with many errors the last one is usually the root cause.
    return " | ".join(errors[-limit:])[:500]


def _find_terminal_exe(ti) -> Optional[Path]:
    """terminal64.exe inside the install dir reported by terminal_info()."""
    path = getattr(ti, "path", None)
    if not path:
        return None
    p = Path(path)
    exe = p / "terminal64.exe"
    if exe.exists():
        return exe
    # Some installs keep terminal64.exe one level up from MQL5 dirs
    alt = p.parent / "terminal64.exe"
    if alt.exists():
        return alt
    return None


def _find_metaeditor(ti) -> Optional[Path]:
    """MetaEditor64.exe next to the terminal; glob MetaEditor*.exe as fallback
    (plan C.3)."""
    path = getattr(ti, "path", None)
    if not path:
        return None
    p = Path(path)
    direct = p / "MetaEditor64.exe"
    if direct.exists():
        return direct
    for cand in sorted(p.glob("MetaEditor*.exe")):
        return cand
    for cand in sorted(p.parent.glob("MetaEditor*.exe")):
        return cand
    return None


def _compile(exe_metaeditor: Path, src: Path, timeout: float) -> tuple[bool, str]:
    """Compile src with MetaEditor; success = a fresh .ex5 next to the source.

    Command is built as ONE string with explicit quotes because MetaEditor
    expects /compile:"path" (plan §4.5). Returns (ok, log_text).
    """
    log_path = src.with_suffix(".log")
    log_path.unlink(missing_ok=True)
    ex5 = src.with_suffix(".ex5")
    ex5_mtime_before = ex5.stat().st_mtime if ex5.exists() else 0.0
    cmd = f'"{exe_metaeditor}" /compile:"{src}" /log'
    logger.info(f"EA compile: {cmd}")
    try:
        subprocess.run(cmd, shell=True, timeout=timeout,
                       capture_output=True)
    except subprocess.TimeoutExpired:
        return False, f"MetaEditor timed out after {timeout:.0f}s"

    # Judge success by a fresh .ex5, not by parsing the log (plan §4.1)
    ok = ex5.exists() and ex5.stat().st_mtime >= ex5_mtime_before
    detail = ""
    if log_path.exists():
        detail = _decode_log(log_path.read_bytes())
        for line in detail.splitlines():
            line = line.strip()
            if not line:
                continue
            (logger.info if ok else logger.error)(f"EA compile log: {line}")
    return ok, detail


def _write_ini(path: Path, login: str, password: str, server: str,
               symbol: str, period: str):
    """ASCII ini for the /config relaunch. NEVER log its contents."""
    path.write_text(
        "[Common]\n"
        f"Login={login}\n"
        f"Password={password}\n"
        f"Server={server}\n"
        "\n"
        "[Experts]\n"
        "AllowLiveTrading=1\n"
        "Enabled=1\n"
        "\n"
        "[StartUp]\n"
        "Expert=WTExecutor\n"
        f"Symbol={symbol}\n"
        f"Period={period}\n",
        encoding="ascii",
    )


def _find_terminal_pid(exe: Path) -> Optional[int]:
    """PID of the running terminal whose exe matches exactly — do not touch
    other brokers' terminals (plan C.6)."""
    try:
        import psutil
    except ImportError:
        logger.error("psutil not available — cannot locate terminal process")
        return None
    target = str(exe).lower()
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            exe_path = proc.info.get("exe")
            if exe_path and exe_path.lower() == target:
                return proc.info["pid"]
        except Exception:
            continue
    return None


def _close_terminal(exe: Path) -> bool:
    """Graceful close via taskkill (no /F), then terminate as fallback."""
    pid = _find_terminal_pid(exe)
    if pid is None:
        return True   # not running; nothing to close
    logger.info(f"Closing MT5 terminal (pid {pid}) for EA attach relaunch")
    try:
        subprocess.run(["taskkill", "/PID", str(pid)], timeout=25,
                       capture_output=True)
    except Exception as e:
        logger.warning(f"taskkill failed: {e}")
    try:
        import psutil
        proc = psutil.Process(pid)
        try:
            proc.wait(timeout=20)
            return True
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
            return True
        except Exception:
            logger.error(f"Terminal pid {pid} refused to terminate")
            return False
    except Exception as e:
        logger.error(f"Terminal close check failed: {e}")
        return False


def _relaunch(exe: Path, ini_path: Path) -> bool:
    """Start the terminal detached so it outlives the bot (plan C.6)."""
    cmd = f'"{exe}" /config:"{ini_path}"'
    # Portable flag if data_path == install path (plan §4.4)
    try:
        ti = mt5.terminal_info()
        if ti is not None and ti.data_path and ti.path and \
                Path(ti.data_path) == Path(ti.path):
            cmd += " /portable"
    except Exception:
        pass
    flags = 0
    if os.name == "nt":
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    logger.info(f"Relaunching MT5 with EA attached: {cmd}")
    try:
        subprocess.Popen(cmd, cwd=str(exe.parent), shell=not os.name == "nt",
                         creationflags=flags, close_fds=True)
        return True
    except Exception as e:
        logger.error(f"Terminal relaunch failed: {e}")
        return False


async def _wait_terminal_and_ea(bridge, symbol: str, period: str) -> Optional[str]:
    """Wait for the terminal to come up, log in via the shared init path, then
    ping the EA. Returns the EA version, or None."""
    # Wait for terminal (slow machines: plan allows 90 s)
    deadline = asyncio.get_event_loop().time() + RELAUNCH_WAIT_S
    while asyncio.get_event_loop().time() < deadline:
        try:
            if mt5.initialize():
                break
        except Exception:
            pass
        time_left = deadline - asyncio.get_event_loop().time()
        if time_left <= 0:
            return None
        await asyncio.sleep(2.0)

    from core.trading_engine import init_mt5_connection
    if not init_mt5_connection():
        logger.error("MT5 login failed after relaunch")
        return None

    # Verify the account the terminal actually logged into (plan §4.2)
    ai = mt5.account_info()
    want = os.getenv("MT5_LOGIN", "")
    if ai is not None and str(ai.login) != str(want):
        logger.error(
            f"MT5 account after relaunch is {ai.login}, expected {want} — "
            "the config launch did not keep the saved login")

    bridge.invalidate()   # common folder may have changed with the relaunch

    # Wait for the EA to answer (plan allows 30 s)
    ea_deadline = asyncio.get_event_loop().time() + PING_WAIT_S
    while asyncio.get_event_loop().time() < ea_deadline:
        ver = _sync_ping(bridge)
        if ver:
            return ver
        time_left = ea_deadline - asyncio.get_event_loop().time()
        if time_left <= 0:
            return None
        await asyncio.sleep(0.5)
    return None


def _sync_ping(bridge) -> Optional[str]:
    """Blocking ping with a short timeout, run via asyncio.to_thread."""
    import concurrent.futures

    def _do():
        import time as _time
        # Reuse the bridge's file protocol synchronously (bridge methods are
        # async but only touch files + a small sleep loop).
        import asyncio as _asyncio
        return _asyncio.run(bridge.ping(timeout_s=2.0))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_do).result(timeout=5.0)
    except Exception:
        return None


async def ensure_ea_ready(bridge, allow_restart: bool) -> EAStatus:
    """Entry point (plan C): install + compile + ping (+ optional relaunch).
    Never raises."""
    try:
        return await _ensure(bridge, allow_restart)
    except Exception as e:
        logger.error(f"EA provisioning failed: {e}")
        return EAStatus(available=False, reason=f"provisioning error: {e}")


async def _ensure(bridge, allow_restart: bool) -> EAStatus:
    # 1. MT5 must be initialised (reuse the engine's exact init/login path)
    ti = mt5.terminal_info()
    if ti is None:
        from core.trading_engine import init_mt5_connection
        if not init_mt5_connection():
            return EAStatus(available=False,
                            reason="MT5 not initialised and init failed")
        ti = mt5.terminal_info()
        if ti is None:
            return EAStatus(available=False,
                            reason="MT5 terminal_info() unavailable after init")

    # 2. Install: copy source only if missing or content differs (plan C.2)
    data_path = Path(ti.data_path or ti.path)
    experts_dir = data_path / "MQL5" / "Experts"
    dest = experts_dir / "WTExecutor.mq5"
    copied = False
    try:
        experts_dir.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or _sha256(dest) != _sha256(EA_SOURCE):
            dest.write_bytes(EA_SOURCE.read_bytes())
            copied = True
            logger.info(f"EA source installed to {dest}")
    except Exception as e:
        return EAStatus(available=False, reason=f"EA install failed: {e}")

    # 3. Compile when .ex5 is missing/older than the source or just copied
    ex5 = dest.with_suffix(".ex5")
    needs_compile = (copied or not ex5.exists()
                     or ex5.stat().st_mtime < dest.stat().st_mtime)
    source_changed = needs_compile
    if needs_compile:
        metaeditor = _find_metaeditor(ti)
        if metaeditor is None:
            return EAStatus(available=False,
                            reason="MetaEditor64.exe not found in MT5 install dir")
        ok, log_text = await asyncio.to_thread(_compile, metaeditor, dest,
                                              COMPILE_TIMEOUT)
        if not ok:
            # Put the compiler's own words in the reason. Leaving a stale .ex5
            # in place is deliberate: MT5 may still have it attached and
            # deleting someone's working build to make a point helps nobody.
            errs = _compile_errors(log_text)
            reason = "EA compile failed"
            if errs:
                reason += f": {errs}"
            else:
                reason += " (no .ex5 produced; see the EA compile log)"
            return EAStatus(available=False, reason=reason)
        logger.info("EA compiled OK")

    # 4. Ping
    ver = await asyncio.to_thread(_sync_ping, bridge)
    if ver == WT_EA_VERSION:
        logger.info(f"EA ready v{ver}")
        return EAStatus(available=True, version=ver)

    # A source update can compile successfully while the terminal continues
    # running the previous EA instance.  If it is safe to restart, do that
    # automatically so a changed executor is never silently rejected and the
    # operator does not have to detach/reattach the chart by hand.
    if ver and source_changed:
        logger.warning(
            f"EA attached reports v{ver} after source update; "
            f"expected v{WT_EA_VERSION} — restarting terminal to load it")
    elif ver:
        logger.warning(
            f"EA attached reports v{ver} but repo source is v{WT_EA_VERSION} "
            "(stale compiled EA attached)")
        return EAStatus(available=True, version=ver,
                        stale=(ver != WT_EA_VERSION))

    # 5. Ping failed — restart only when the account has zero open positions
    # (plan decision in section 1) and only when allowed
    if not allow_restart:
        return EAStatus(available=False,
                        reason="EA not answering and auto-restart disabled")
    try:
        n_pos = mt5.positions_total()
    except Exception:
        n_pos = None
    if n_pos is None:
        return EAStatus(available=False,
                        reason="EA not answering and positions_total() unknown (None)")
    if n_pos != 0:
        return EAStatus(
            available=False,
            reason=f"EA not answering and {n_pos} position(s) open — no restart")

    # 6. Relaunch with the EA attached
    exe = _find_terminal_exe(ti)
    if exe is None:
        return EAStatus(available=False,
                        reason="terminal64.exe not found in MT5 install dir")

    login = os.getenv("MT5_LOGIN", "")
    password = os.getenv("MT5_PASSWORD", "")
    server = os.getenv("MT5_SERVER", "")
    if not (login and password and server):
        return EAStatus(available=False,
                        reason="MT5_LOGIN/PASSWORD/SERVER not set — cannot relaunch")

    symbol = os.getenv("EA_CHART_SYMBOL", "FX Vol 20")
    period = os.getenv("EA_CHART_PERIOD", "M1")

    ini_path = Path(tempfile.gettempdir()) / f"wt_ea_attach_{os.getpid()}.ini"
    # Delete a stale ini from an earlier run first, and always in finally
    try:
        ini_path.unlink(missing_ok=True)
    except Exception:
        pass
    _write_ini(ini_path, login, password, server, symbol, period)

    try:
        mt5.shutdown()
        if not _close_terminal(exe):
            return EAStatus(available=False,
                            reason="could not close running MT5 terminal")
        if not _relaunch(exe, ini_path):
            return EAStatus(available=False, reason="MT5 relaunch failed")

        ver = await _wait_terminal_and_ea(bridge, symbol, period)
        if ver is None:
            return EAStatus(available=False,
                            reason="EA still not answering after relaunch")
        if ver != WT_EA_VERSION:
            logger.warning(
                f"EA attached reports v{ver} but repo source is v{WT_EA_VERSION}")
        logger.info(f"EA ready v{ver}")
        return EAStatus(available=True, version=ver, stale=(ver != WT_EA_VERSION))
    finally:
        # The ini holds the password — ALWAYS delete it, never log it
        try:
            ini_path.unlink(missing_ok=True)
        except Exception:
            pass
