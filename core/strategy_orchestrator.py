"""Strategy orchestration for the Queued Close engine."""

# pyright: reportAttributeAccessIssue=false

from typing import Any, Dict, List, Set
import asyncio
import time
from core.engine.queued_close_strategy_engine import QueuedCloseStrategyEngine as QCStrategy
from core.session_logger import SessionLogger
from core.bulk_orders import MAGIC_NUMBER
from core.run_state import run_state_manager


class StrategyOrchestrator:
    """
    Per-user orchestrator that manages multiple strategies (one per symbol).
    Works with the new multi-asset config structure.
    Includes session logging for transparency and debugging.
    """
    
    def __init__(self, config_manager, user_id: str = "default"):
        self.config_manager = config_manager
        self.user_id = user_id
        # Map symbol -> QCStrategy
        self.strategies: Dict[str, QCStrategy] = {}
        self.active_symbols: Set[str] = set()
        
        # Session Logger for history tracking
        self.session_logger = SessionLogger(user_id)
        
        # Initialize
        self.update_strategies()

    @property
    def config(self):
        """Pass-through to config manager for the API"""
        return self.config_manager.get_config()

    def update_strategies(self):
        """
        Syncs active strategies with the configuration.
        Spawns new bots for enabled symbols, removes disabled ones.
        """
        # Get enabled symbols from new config structure
        enabled_symbols = set(self.config_manager.get_enabled_symbols())
        current_symbols = set(self.strategies.keys())

        # 1. Remove disabled symbols
        to_remove = current_symbols - enabled_symbols
        for sym in to_remove:
            print(f"[ORCHESTRATOR] Stopping Strategy: {sym}")
            strategy = self.strategies.get(sym)
            if strategy and strategy.running:
                # Best-effort graceful stop before dropping the reference.
                asyncio.create_task(strategy.stop())
            del self.strategies[sym]

        # 2. Add newly enabled symbols
        to_add = enabled_symbols - current_symbols
        for sym in to_add:
            sym_config = self.config_manager.get_symbol_config(sym)
            if sym_config:
                print(f"[ORCHESTRATOR] Spawning Strategy: {sym}")
                strategy = QCStrategy(self.config_manager, sym, self.user_id, session_logger=self.session_logger)
                self._attach_ea(strategy)
                self.strategies[sym] = strategy

        self.active_symbols = enabled_symbols

    def _attach_ea(self, strategy):
        """Give a new strategy the shared EA bridge/status (plan phase E).
        Reads the class-level BotManager handle — no import cycle."""
        from core.bot_manager import BotManager   # local import: avoid cycle
        holder = getattr(BotManager, "_last_instance", None)
        if holder is not None and holder.ea_bridge is not None:
            strategy.ea_bridge = holder.ea_bridge
            strategy.ea_status = holder.ea_status

    async def reconcile_strategies_on_startup(self):
        """Run a one-time startup reconciliation for every instantiated strategy."""
        summaries = {}
        for symbol, strategy in self.strategies.items():
            summaries[symbol] = await strategy.reconcile_on_startup()
        return summaries

    async def start(self):
        """Start all enabled strategies"""
        self.update_strategies()
        self.session_logger.log_button("Start All")
        tasks = [bot.start() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def stop(self):
        """Stop all strategies (graceful - completes open pairs)"""
        self.session_logger.log_button("Graceful Stop All")
        tasks = [bot.stop() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def start_symbol(self, symbol: str):
        """Start a specific symbol strategy"""
        if symbol not in self.strategies:
            sym_config = self.config_manager.get_symbol_config(symbol)
            if sym_config and sym_config.get('enabled', False):
                print(f"[ORCHESTRATOR] Spawning Strategy: {symbol}")
                strategy = QCStrategy(self.config_manager, symbol, self.user_id, session_logger=self.session_logger)
                self.strategies[symbol] = strategy
                self.active_symbols.add(symbol)
        
        if symbol in self.strategies:
            self._attach_ea(self.strategies[symbol])
            self.session_logger.log_button(f"Start {symbol}")
            await self.strategies[symbol].start()

    async def stop_symbol(self, symbol: str):
        """Stop a specific symbol strategy (graceful)"""
        if symbol in self.strategies:
            self.session_logger.log_button(f"Stop {symbol}")
            await self.strategies[symbol].stop()
            if not self.strategies[symbol].running:
                del self.strategies[symbol]
                self.active_symbols.discard(symbol)

    async def terminate_symbol(self, symbol: str):
        """
        Nuclear reset - close all positions for a symbol immediately.
        Calls terminate() on the strategy which closes all positions and resets grid.
        """
        print(f"[TERMINATE] Starting terminate for {symbol}")
        if symbol in self.strategies:
            self.session_logger.log_button(f"Terminate {symbol}")
            await self.strategies[symbol].terminate()
            del self.strategies[symbol]
            self.active_symbols.discard(symbol)
            print(f"[TERMINATE] {symbol}: Strategy terminated and removed.")
        else:
            print(f"[TERMINATE] {symbol}: Strategy not found in active strategies.")

    async def terminate_all(self):
        """
        Nuclear reset independent of the in-memory strategy registry.

        The registry is only an optimization: after a restart, strategies may
        not have been instantiated while their EA-owned orders are still live.
        Discover every symbol in config and in the terminal, cancel our
        pendings, close our positions, and verify the account is clear.
        """
        self.session_logger.log_button("Terminate All")
        import MetaTrader5 as mt5
        configured = set(self.config_manager.get_enabled_symbols())
        symbols = configured | set(self.strategies)
        positions = list(mt5.positions_get() or ())
        orders = list(mt5.orders_get() or ())
        symbols |= {p.symbol for p in positions if p.magic == MAGIC_NUMBER}
        symbols |= {o.symbol for o in orders if o.magic == MAGIC_NUMBER}
        initial_positions = sum(p.magic == MAGIC_NUMBER for p in positions)
        initial_pending = sum(o.magic == MAGIC_NUMBER for o in orders)

        holder = getattr(__import__("core.bot_manager", fromlist=["BotManager"]),
                         "BotManager", None)
        bridge = getattr(holder, "_last_instance", None)
        bridge = getattr(bridge, "ea_bridge", None)

        # Stop the EA arm machines before touching positions, otherwise a
        # trigger can create a fresh position during the close pass.
        if bridge is not None:
            await asyncio.gather(*(
                bridge.abort_arm(symbol, MAGIC_NUMBER) for symbol in symbols
            ), return_exceptions=True)

        async def terminate_registered(strategy):
            try:
                await strategy.terminate()
            except Exception as exc:
                print(f"[TERMINATE ALL] strategy cleanup failed: {exc}")

        await asyncio.gather(*(terminate_registered(s)
                               for s in list(self.strategies.values())))

        deadline = time.monotonic() + 15.0
        while True:
            orders = list(mt5.orders_get() or ())
            mine_orders = [o for o in orders if o.magic == MAGIC_NUMBER]
            for symbol in {o.symbol for o in mine_orders} | symbols:
                if bridge is not None:
                    try:
                        await bridge.cancel_all_pendings(symbol, MAGIC_NUMBER,
                                                         deadline_s=2.0)
                    except Exception:
                        pass
                else:
                    for order in mine_orders:
                        if order.symbol != symbol:
                            continue
                        mt5.order_send({
                            "action": mt5.TRADE_ACTION_REMOVE,
                            "symbol": symbol, "order": order.ticket,
                            "magic": MAGIC_NUMBER, "comment": "terminate-all",
                        })

            positions = list(mt5.positions_get() or ())
            mine_positions = [p for p in positions if p.magic == MAGIC_NUMBER]
            by_symbol = {}
            for position in mine_positions:
                by_symbol.setdefault(position.symbol, []).append(position)
            for symbol, items in by_symbol.items():
                tickets = [p.ticket for p in items]
                closed = False
                if bridge is not None:
                    try:
                        await bridge.close_tickets(symbol, MAGIC_NUMBER, tickets)
                        live = {p.ticket for p in (mt5.positions_get(symbol=symbol) or ())
                                if p.magic == MAGIC_NUMBER}
                        closed = not live.intersection(tickets)
                    except Exception:
                        pass
                if not closed:
                    for position in items:
                        tick = mt5.symbol_info_tick(symbol)
                        if not tick:
                            continue
                        close_type = (mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY
                                      else mt5.ORDER_TYPE_BUY)
                        mt5.order_send({
                            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                            "position": position.ticket, "volume": position.volume,
                            "type": close_type,
                            "price": tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask,
                            "deviation": 50, "magic": MAGIC_NUMBER,
                            "comment": "Terminate-All",
                        })

            await asyncio.sleep(0.25)
            remaining_orders = [o for o in (mt5.orders_get() or ())
                                if o.magic == MAGIC_NUMBER]
            remaining_positions = [p for p in (mt5.positions_get() or ())
                                   if p.magic == MAGIC_NUMBER]
            if not remaining_orders and not remaining_positions:
                break
            if time.monotonic() >= deadline:
                break

        # Clear persisted arm/run state even when no strategy was registered.
        if bridge is not None:
            try:
                bridge.clear_limit_phase_files()
            except Exception:
                pass
        run_state_manager.set_stopped(self.user_id)
        self.strategies.clear()
        self.active_symbols.clear()
        leftovers = {
            "pending": len([o for o in (mt5.orders_get() or ())
                            if o.magic == MAGIC_NUMBER]),
            "positions": len([p for p in (mt5.positions_get() or ())
                              if p.magic == MAGIC_NUMBER]),
        }
        return {
            "initial_pending": initial_pending,
            "initial_positions": initial_positions,
            "cancelled_pending": initial_pending - leftovers["pending"],
            "closed_positions": initial_positions - leftovers["positions"],
            "leftover_pending": leftovers["pending"],
            "leftover_positions": leftovers["positions"],
            "timed_out": bool(leftovers["pending"] or leftovers["positions"]),
        }

    async def close(self):
        """Release any open resources held by strategies and repositories."""
        tasks = []
        for strategy in self.strategies.values():
            close_fn = getattr(strategy, "close", None)
            if close_fn is not None:
                tasks.append(close_fn())

        if tasks:
            await asyncio.gather(*tasks)

        self.strategies.clear()
        self.active_symbols.clear()

    async def start_ticker(self):
        """
        Called when config updates. Re-syncs strategies and notifies them.
        """
        self.update_strategies()
        tasks = [bot.start_ticker() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def on_external_tick(self, symbol, tick_data):
        """Routes the tick to the specific strategy for this symbol."""
        if symbol in self.strategies:
            await self.strategies[symbol].on_external_tick(tick_data)

    def get_active_symbols(self) -> List[str]:
        """Returns symbols that are currently active AND running."""
        return [sym for sym, strategy in self.strategies.items() if strategy.running]

    def get_status(self) -> Dict[str, Any]:
        """
        Returns status for all active strategies.
        For multi-asset, returns per-symbol status in a 'strategies' dict.
        """
        if not self.strategies:
            return {
                "running": False,
                "graceful_stop": False,
                "current_price": 0,
                "open_positions": 0,
                "step": 0,
                "iteration": 0,
                "is_resetting": False,
                "armed": False,
                "strategies": {}
            }

        # Aggregate stats
        total_positions = 0
        running_any = False
        is_resetting_any = False
        graceful_stop_any = False
        # Armed ladders are reported per symbol; the top-level flag is true
        # when ANY symbol is waiting on its trigger, so the UI can say so
        # without having to know which symbol it is.
        armed_any = False
        per_symbol_status = {}
        
        for symbol, bot in self.strategies.items():
            s = bot.get_status()
            per_symbol_status[symbol] = s
            total_positions += s.get('open_positions', 0)
            if s.get('running', False):
                running_any = True
            if s.get('is_resetting', False):
                is_resetting_any = True
            if s.get('graceful_stop', False):
                graceful_stop_any = True
            if s.get('armed', False):
                armed_any = True
        
        # For backward compatibility, use first bot for single-value fields
        first_bot = list(self.strategies.values())[0] if self.strategies else None
        first_status = first_bot.get_status() if first_bot else {}

        # Aggregate moving/constant split (Queued Close status concept)
        moving_total = sum(s.get('moving_total', 0) for s in per_symbol_status.values())
        constant_total = sum(s.get('constant_total', 0) for s in per_symbol_status.values())
        moving_closed = sum(s.get('moving_closed', 0) for s in per_symbol_status.values())
        constant_closed = sum(s.get('constant_closed', 0) for s in per_symbol_status.values())

        return {
            "running": running_any,
            "graceful_stop": graceful_stop_any,
            "current_price": first_bot.current_price if first_bot else 0,
            "open_positions": total_positions,
            "step": first_status.get('step', 0),
            "iteration": first_status.get('iteration', 0),
            "is_resetting": is_resetting_any,
            "armed": armed_any,
            "moving_total": moving_total,
            "constant_total": constant_total,
            "moving_closed": moving_closed,
            "constant_closed": constant_closed,
            "queue_length": sum(s.get('queue_length', 0) for s in per_symbol_status.values()),
            "catching_up": any(s.get('catching_up', False) for s in per_symbol_status.values()),
            "active_count": len(self.strategies),
            "strategies": per_symbol_status
        }
