import uuid
from typing import Dict, Optional
from core.config_manager import ConfigManager
from core.strategy_orchestrator import StrategyOrchestrator

class BotManager:
    def __init__(self):
        # Maps user_id -> StrategyOrchestrator
        self.bots: Dict[str, StrategyOrchestrator] = {}
        # EA bulk-open support (plan phase E): injected by the server startup
        # hook; every strategy created afterwards receives these references.
        self.ea_bridge = None
        self.ea_status = None
        # Class-level handle so orchestrators can propagate the EA refs to
        # strategies they spawn (set in set_ea's callers via the instance).
        BotManager._last_instance = self

    def set_ea(self, ea_bridge, ea_status):
        """Store the EA bridge/status and propagate to all live strategies."""
        self.ea_bridge = ea_bridge
        self.ea_status = ea_status
        for orch in self.bots.values():
            for strategy in orch.strategies.values():
                strategy.ea_bridge = ea_bridge
                strategy.ea_status = ea_status

    async def get_or_create_bot(self, user_id: str) -> StrategyOrchestrator:
        """
        Retrieves an existing bot orchestrator for the user, or creates a new one 
        if the server restarted or it doesn't exist.
        """
        # 1. Return existing instance if in memory
        if user_id in self.bots:
            return self.bots[user_id]
        
        # 2. Re-initialize bot for this user (restores config from DB/File)
        print(f"[BOT] Restoring/Creating bot session for User: {user_id}")
        config_manager = ConfigManager(user_id=user_id)
        
        # Initialize Strategy Orchestrator with user_id for session logging
        orchestrator = StrategyOrchestrator(config_manager, user_id=user_id)

        # Reconcile live MT5 positions against persisted state before any ticker sync.
        await orchestrator.reconcile_strategies_on_startup()

        # Propagate EA bridge/status to any strategies just created
        if self.ea_bridge is not None:
            for strategy in orchestrator.strategies.values():
                strategy.ea_bridge = self.ea_bridge
                strategy.ea_status = self.ea_status

        # Start Ticker (Passive) - Actually for Orchestrator this syncs strategies
        await orchestrator.start_ticker()
        
        # Store in memory
        self.bots[user_id] = orchestrator
        return orchestrator

    def get_bot(self, user_id: str) -> Optional[StrategyOrchestrator]:
        return self.bots.get(user_id)

    async def stop_bot(self, user_id: str):
        bot = self.bots.get(user_id)
        if bot:
            await bot.stop()
            print(f"Bot stopped for user: {user_id}")

    async def stop_all(self):
        for user_id in list(self.bots.keys()):
            await self.stop_bot(user_id)