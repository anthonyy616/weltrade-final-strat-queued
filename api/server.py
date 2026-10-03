from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Any, Optional
from core.bot_manager import BotManager
from core.trading_engine import TradingEngine 
from supabase import create_client, Client
import asyncio
import os
import signal
import sys
from dotenv import load_dotenv
from cachetools import TTLCache 
import gc

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - deployment dependency fallback
    psutil = None

load_dotenv()

# --- EA bridge / provisioner / log tail (plan phases C–E) ---
from core.ea_bridge import EABridge
from core.ea_provisioner import ensure_ea_ready
from core.ea_log_tail import EALogTail

# Shared EA status; the strategy reads this to pick bulk vs sequential.
ea_bridge = EABridge()
ea_status = {"available": False, "version": None, "reason": "not provisioned yet"}


# --- Persistence ---
# The DB is NOT wiped on boot: it holds the state crash recovery reconciles
# against (open tickets, queue, cycle counters). Deleting it here made
# reconcile_on_startup a no-op and left live positions unmanaged after a
# restart. A deliberate reset is /control/terminate-all only.
DB_PATH = "db/grid_v3.db"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Auth Cache (30 seconds - shorter TTL for multi-user support)
auth_cache = TTLCache(maxsize=100, ttl=120)

# --- 1. Initialize Core Systems ---
bot_manager = BotManager()
trading_engine = TradingEngine(bot_manager)

@app.on_event("startup")
async def startup_event():
    print("[SERVER] Starting: Launching Monolith Engine...")

    # Provision the EA BEFORE the trading engine starts (plan E.1). A stale
    # ini holding the password is deleted by the provisioner itself; the log
    # tail never prints credentials — it only reads the EA's own wt_ea.log.
    allow_restart = os.getenv("EA_AUTO_RESTART", "true").lower() != "false"
    try:
        status = await ensure_ea_ready(ea_bridge, allow_restart=allow_restart)
        ea_status["available"] = status.available
        ea_status["version"] = status.version
        ea_status["reason"] = status.reason
        if status.available:
            print(f"[SERVER] EA ready v{status.version}")
        else:
            print(f"[SERVER] EA unavailable: {status.reason} (sequential fallback active)")
    except Exception as e:
        ea_status["reason"] = f"provisioning error: {e}"
        print(f"[SERVER] EA unavailable: {e} (sequential fallback active)")

    ea_log_tail = EALogTail(ea_bridge)
    ea_log_tail.start()

    # Give every strategy engine access to the bridge + status (plan E.1)
    bot_manager.set_ea(ea_bridge, ea_status)

    asyncio.create_task(trading_engine.start())


# --- Pydantic Models for Config ---

class SymbolConfig(BaseModel):
    """Config for a single symbol (Queued Close Strategy)"""
    enabled: Optional[bool] = None
    buy_count: Optional[int] = None
    sell_count: Optional[int] = None
    buy_lot: Optional[float] = None
    sell_lot: Optional[float] = None
    constant_side: Optional[str] = None
    grid_distance: Optional[float] = None
    moving_freq: Optional[float] = None
    constant_freq: Optional[float] = None
    # Limit-trigger open mode (doc 08 §4). Declared here explicitly because
    # Pydantic silently drops unknown keys, so a missing field here means the
    # value never reaches ConfigManager.
    open_mode: Optional[str] = None
    entry_offset: Optional[float] = None

class GlobalConfig(BaseModel):
    """Global settings"""
    max_runtime_minutes: Optional[int] = None
    volatility_tolerance: Optional[str] = None  # accepted but unused in this fork
    # Limit-trigger globals (doc 08 §4)
    armed_timeout_seconds: Optional[int] = None
    win_fill_deadline_ms: Optional[int] = None
    cancel_ack_deadline_ms: Optional[int] = None
    burst_mode: Optional[str] = None
    max_consecutive_open_failures: Optional[int] = None

class ConfigUpdate(BaseModel):
    """Multi-asset config update payload"""
    model_config = ConfigDict(populate_by_name=True)
    # New preferred key used by frontend
    global_: Optional[GlobalConfig] = Field(default=None, alias="global")
    global_settings: Optional[GlobalConfig] = None
    symbols: Optional[Dict[str, SymbolConfig]] = None


# --- 2. Auth Helper ---
def verify_token_sync(token):
    """
    Verify Supabase token with short-term caching.
    Cache by token for 30 seconds to reduce API calls while allowing multiple users.
    """
    if token in auth_cache: 
        return auth_cache[token]
    
    try:
        user = supabase.auth.get_user(token)
        if user and user.user:
            auth_cache[token] = user
            return user
    except Exception as e:
        print(f"[AUTH] Token validation error: {e}")
        # Remove from cache if validation failed
        if token in auth_cache:
            del auth_cache[token]
    return None

async def get_current_bot(request: Request):
    """
    Get or create bot instance for the authenticated user.
    Each user gets their own isolated bot instance.
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Missing token")

    try:
        token = auth_header.split(" ", 1)[1]
        if not token:
            raise ValueError("empty bearer token")
        user = await asyncio.to_thread(verify_token_sync, token)
    except Exception as e:
        print(f"[AUTH] Check Failed: {e}")
        raise HTTPException(401, "Auth Validation Failed")

    if not user: 
        raise HTTPException(401, "Invalid Token")
    
    # Each user gets their own bot instance (multi-tenant support)
    return await bot_manager.get_or_create_bot(user.user.id)

# --- 3. API Routes (Defined BEFORE Static Mount) ---

@app.get("/env")
async def get_env():
    return { "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY }

@app.get("/health", status_code=200)
@app.head("/health", status_code=200)
async def health_check():
    """Lightweight health check for VPS monitoring (GET/HEAD)"""
    return {"status": "ok"}

@app.get("/config")
async def get_config(bot = Depends(get_current_bot)):
    """Get full multi-asset config"""
    return bot.config

@app.post("/config")
async def update_config(config: ConfigUpdate, bot = Depends(get_current_bot)):
    """Update multi-asset config"""
    update_data = {}
    
    # Handle global settings
    global_cfg = config.global_ or config.global_settings
    if global_cfg:
        update_data["global"] = {
            k: v for k, v in global_cfg.model_dump().items() 
            if v is not None
        }
    
    # Handle symbol-specific settings
    if config.symbols:
        update_data["symbols"] = {}
        for symbol, sym_cfg in config.symbols.items():
            sym_data = {k: v for k, v in sym_cfg.model_dump().items() if v is not None}
            if sym_data:
                update_data["symbols"][symbol] = sym_data
    
    updated = bot.config_manager.update_config(update_data)
    return updated


# --- Per-Symbol Control Endpoints ---


async def _prepare_fresh_session(bot):
    """Ensure all bot resources are closed before replacing the SQLite file."""
    try:
        if hasattr(bot, "terminate_all"):
            await bot.terminate_all()
        elif hasattr(bot, "stop"):
            await bot.stop()
    finally:
        close_fn = getattr(bot, "close", None)
        if close_fn is not None:
            await close_fn()
        gc.collect()


async def _delete_db_file() -> bool:
    if not os.path.exists(DB_PATH):
        return True

    retry_count = 0
    max_retries = 3

    while retry_count < max_retries:
        try:
            os.remove(DB_PATH)
            print(f"[START] Cleaned DB for fresh session: {DB_PATH}")
            return True
        except PermissionError as e:
            retry_count += 1
            print(f"[START] DB delete attempt {retry_count} failed: {e}")
            if retry_count < max_retries:
                await asyncio.sleep(0.5)
        except Exception as e:
            retry_count += 1
            print(f"[START] DB delete attempt {retry_count} failed: {e}")
            if retry_count < max_retries:
                await asyncio.sleep(0.5)

    await _release_db_locks(DB_PATH)
    await asyncio.sleep(0.5)

    try:
        os.remove(DB_PATH)
        print(f"[START] Cleaned DB after releasing locks: {DB_PATH}")
        return True
    except Exception as e:
        print(f"[START] Final DB delete failed: {e}")

    return not os.path.exists(DB_PATH)


async def _release_db_locks(db_path: str) -> None:
    """Terminate only the processes that currently hold the DB file open."""
    if psutil is None:
        return

    target_path = os.path.abspath(db_path)
    locked_pids = set()
    current_pid = os.getpid()

    for proc in psutil.process_iter(["pid", "name", "open_files"]):
        try:
            open_files = proc.info.get("open_files") or []
            for file_info in open_files:
                try:
                    if os.path.abspath(file_info.path) == target_path:
                        pid = proc.info["pid"]
                        if pid != current_pid:
                            locked_pids.add(pid)
                        break
                except Exception:
                    continue
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if not locked_pids:
        return

    print(f"[START] DB still locked by PIDs: {sorted(locked_pids)}. Releasing holders...")

    for pid in locked_pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    try:
        _, alive = psutil.wait_procs([psutil.Process(pid) for pid in locked_pids if psutil.pid_exists(pid)], timeout=2)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        await asyncio.sleep(0.5)
    except Exception as e:
        print(f"[START] Process release wait failed: {e}")

@app.post("/control/start")
async def start_all(bot = Depends(get_current_bot)):
    """Start all enabled symbols - always starts with fresh DB"""
    await _prepare_fresh_session(bot)
    deleted = await _delete_db_file()

    if not deleted and os.path.exists(DB_PATH):
        raise HTTPException(
            status_code=409,
            detail="DB file is still locked after closing the bot session. Stop the process holding the file and try again."
        )
    
    # [FIX] Auto-Restart Trading Engine if stopped
    if not trading_engine.running:
        print("[SERVER] Restarting Trading Engine...")
        asyncio.create_task(trading_engine.start())
        
        # Wait for engine to initialize (up to 5s)
        for _ in range(10):
            if trading_engine.running:
                break
            await asyncio.sleep(0.5)
        
    await bot.start()
    return {"status": "started", "symbols": bot.config_manager.get_enabled_symbols()}

@app.post("/control/stop")
async def stop_all(bot = Depends(get_current_bot)):
    """Stop all symbols"""
    await bot.stop()
    return {"status": "stopped"}

@app.post("/control/start/{symbol}")
async def start_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Start a specific symbol"""
    #await _prepare_fresh_session(bot)
    #deleted = await _delete_db_file()

    #if not deleted and os.path.exists(DB_PATH):
        #raise HTTPException(
            #status_code=409,
            #detail="DB file is still locked after closing the bot session. Stop the process holding the file and try again."
        #)
    
    # [FIX] Auto-Restart Trading Engine if stopped
    if not trading_engine.running:
        print("[SERVER] Restarting Trading Engine...")
        asyncio.create_task(trading_engine.start())
        
        # Wait for engine to initialize (up to 5s)
        for _ in range(10):
            if trading_engine.running:
                break
            await asyncio.sleep(0.5)

    # Enable the symbol first
    bot.config_manager.enable_symbol(symbol, True)
    await bot.start_symbol(symbol)
    return {"status": "started", "symbol": symbol}

@app.post("/control/stop/{symbol}")
async def stop_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Stop a specific symbol"""
    await bot.stop_symbol(symbol)
    return {"status": "stopped", "symbol": symbol}

@app.post("/control/terminate/{symbol}")
async def terminate_symbol(symbol: str, bot = Depends(get_current_bot)):
    """Nuclear reset - close all positions for a symbol immediately"""
    await bot.terminate_symbol(symbol)
    return {"status": "terminated", "symbol": symbol}

@app.post("/control/terminate-all")
async def terminate_all(bot = Depends(get_current_bot)):
    """Nuclear reset - close all positions for all symbols and clean DB"""
    await bot.terminate_all()
    
    # Clean DB after termination for complete reset
    db_cleaned = True
    db_warning = None
    
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print(f"[TERMINATE] Cleaned DB after nuclear reset: {DB_PATH}")
        except Exception as e:
            print(f"[TERMINATE] Could not clean DB: {e}")
            db_cleaned = False
            db_warning = f"Could not delete DB file ({e}). Please retry or restart."
    
    return {
        "status": "terminated_all",
        "db_cleaned": db_cleaned,
        "warning": db_warning
    }

@app.get("/status")
async def get_status(bot = Depends(get_current_bot)):
    """Get status for all active strategies"""
    return bot.get_status()

# --- History Endpoints ---

@app.get("/history")
async def get_history(bot = Depends(get_current_bot)):
    """Get list of session history files for this user"""
    sessions = bot.session_logger.get_sessions()
    return sessions

# NOTE: Specific routes MUST come BEFORE parameterized routes in FastAPI
@app.get("/history/groups")
async def get_group_logs(bot = Depends(get_current_bot)):
    """Get list of group log files for this user"""
    from pathlib import Path
    import itertools
    log_dir = bot.session_logger.log_dir
    logs = []
    if log_dir.exists():
        # TASK 3 FIX: Include both .log and .txt file types
        log_files = itertools.chain(
            log_dir.glob("groups_log_*.txt"),  # Table snapshots
            log_dir.glob("groups_*.log"),      # Event logs (Group Strategy)
            log_dir.glob("activity_*.log")     # Activity logs (Pair Strategy)
        )
        for file in sorted(log_files, key=lambda f: f.stat().st_mtime, reverse=True):
            logs.append({
                "id": file.stem,
                "name": file.name,
                "path": str(file)
            })
        # Also include group table files
        for file in sorted(log_dir.glob("group_*_table.txt"), reverse=True):
            logs.append({
                "id": file.stem,
                "name": file.name,
                "path": str(file)
            })
    return logs

@app.get("/history/groups/{filename}")
async def get_group_log_content(filename: str, bot = Depends(get_current_bot)):
    """Get contents of a specific group log file"""
    from pathlib import Path
    from fastapi.responses import PlainTextResponse
    log_dir = bot.session_logger.log_dir
    log_path = log_dir / filename
    if log_path.exists() and log_path.is_file():
        return PlainTextResponse(log_path.read_text(encoding="utf-8"))
    raise HTTPException(404, "Group log not found")

@app.get("/history/{session_id}")
async def get_session_log(session_id: str, bot = Depends(get_current_bot)):
    """Get contents of a specific session log"""
    content = bot.session_logger.get_session_content(session_id)
    if content:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content)
    raise HTTPException(404, "Session not found")

# --- Activity Log Endpoints ---

@app.get("/history/activity")
async def get_activity_logs(bot = Depends(get_current_bot)):
    """Get list of activity log files for this user"""
    from pathlib import Path
    user_id = getattr(bot, 'user_id', 'default')
    log_dir = Path(f"logs/activity/{user_id}")
    logs = []
    if log_dir.exists():
        for file in sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True):
            logs.append({
                "id": file.stem,
                "name": file.name,
                "path": str(file),
                "size": file.stat().st_size,
                "modified": file.stat().st_mtime
            })
    return logs

@app.get("/history/activity/{filename}")
async def get_activity_log_content(filename: str, bot = Depends(get_current_bot)):
    """Get contents of a specific activity log file"""
    from pathlib import Path
    from fastapi.responses import PlainTextResponse
    user_id = getattr(bot, 'user_id', 'default')
    log_dir = Path(f"logs/activity/{user_id}")
    log_path = log_dir / filename
    if log_path.exists() and log_path.is_file():
        return PlainTextResponse(log_path.read_text(encoding="utf-8"))
    raise HTTPException(404, "Activity log not found")

# Mount static folder for assets (css/js images)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve UI at Root (GET/HEAD)
@app.get("/")
@app.head("/")
async def read_index():
    return FileResponse('static/index.html')


# --- 4. Simplified Signal Handling ---
def cleanup_handler(signum, frame):
    """Handle SIGINT (Ctrl+C) - exit cleanly. DB cleanup handled on next startup."""
    print("\n[SERVER] Caught Signal. Exiting...")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup_handler)
print("[SERVER] Signal Handler Registered")