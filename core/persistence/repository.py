# core/persistence/repository.py
import aiosqlite
import logging
import time
from typing import Dict, List, Any, Tuple, Optional
import os

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS symbol_state (
    symbol TEXT PRIMARY KEY,
    phase TEXT NOT NULL DEFAULT 'IDLE',
    center_price REAL DEFAULT 0.0,
    iteration INTEGER DEFAULT 0,
    last_update_time REAL DEFAULT 0,
    cycle_id INTEGER DEFAULT 0,
    anchor_price REAL DEFAULT 0.0,
    metadata TEXT DEFAULT '{}',
    grid_level_1 REAL DEFAULT 0.0,
    grid_level_2 REAL DEFAULT 0.0,
    active_grid_count INTEGER DEFAULT 1,
    position_counter INTEGER DEFAULT 0,
    last_direction TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS grid_pairs (
    symbol TEXT NOT NULL,
    pair_index INTEGER NOT NULL,
    buy_price REAL DEFAULT 0.0,
    sell_price REAL DEFAULT 0.0,
    buy_ticket INTEGER DEFAULT 0,
    sell_ticket INTEGER DEFAULT 0,
    buy_filled INTEGER DEFAULT 0,
    sell_filled INTEGER DEFAULT 0,
    buy_pending_ticket INTEGER DEFAULT 0,
    sell_pending_ticket INTEGER DEFAULT 0,
    trade_count INTEGER DEFAULT 0,
    next_action TEXT DEFAULT 'buy',
    is_reopened INTEGER DEFAULT 0,
    buy_in_zone INTEGER DEFAULT 0,
    sell_in_zone INTEGER DEFAULT 0,
    hedge_ticket INTEGER DEFAULT 0,
    hedge_direction TEXT,
    hedge_active INTEGER DEFAULT 0,
    locked_buy_entry REAL DEFAULT 0.0,
    locked_sell_entry REAL DEFAULT 0.0,
    tp_blocked INTEGER DEFAULT 0,
    group_id INTEGER DEFAULT 0,
    metadata TEXT DEFAULT '{}',
    PRIMARY KEY (symbol, pair_index)
);

CREATE TABLE IF NOT EXISTS ticket_map (
    ticket INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    cycle_id INTEGER DEFAULT 0,
    pair_index INTEGER DEFAULT 0,
    set_index INTEGER DEFAULT 0,
    grid_level INTEGER DEFAULT 0,
    leg TEXT DEFAULT '',
    trade_count INTEGER DEFAULT 0,
    entry_price REAL DEFAULT 0.0,
    tp_price REAL DEFAULT 0.0,
    sl_price REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    pair_index INTEGER DEFAULT 0,
    direction TEXT DEFAULT '',
    price REAL DEFAULT 0.0,
    lot_size REAL DEFAULT 0.0,
    ticket INTEGER DEFAULT 0,
    notes TEXT DEFAULT ''
);

-- Queued Close Strategy tables (agent/02 §9)
CREATE TABLE IF NOT EXISTS moving_positions (
    ticket INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    cycle_id INTEGER DEFAULT 0,
    entry REAL DEFAULT 0.0,
    tp_price REAL DEFAULT 0.0,
    sl_price REAL DEFAULT 0.0,
    direction TEXT DEFAULT '',
    slot_index INTEGER DEFAULT 0,
    closed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS constant_targets (
    symbol TEXT NOT NULL,
    cycle_id INTEGER DEFAULT 0,
    direction TEXT NOT NULL,
    slot_index INTEGER NOT NULL,
    price REAL DEFAULT 0.0,
    fired INTEGER DEFAULT 0,
    PRIMARY KEY (symbol, cycle_id, direction, slot_index)
);

CREATE TABLE IF NOT EXISTS constant_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    cycle_id INTEGER DEFAULT 0,
    direction TEXT DEFAULT '',
    slot_index INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    enqueued_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS constant_tickets (
    ticket INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    cycle_id INTEGER DEFAULT 0
);
"""

# Ensure db directory exists
os.makedirs("db", exist_ok=True)
DB_PATH = "db/grid_v3.db"

class Repository:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.db: Optional[aiosqlite.Connection] = None

    def _conn(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("Repository is not initialized")
        return self.db

    async def initialize(self):
        """Connect and ensure schema exists."""
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        
        # Read schema file, but fall back to built-in bootstrap if it is missing.
        schema_path = os.path.join("db", "schema.sql")
        # Adjust path if running from root or core
        if not os.path.exists(schema_path):
             # Try absolute path based on project root assumption or relative
             current_dir = os.path.dirname(os.path.abspath(__file__))
             # core/persistence/ -> db/schema.sql? No, db is at root usually.
             # Assuming running from root:
             schema_path = "db/schema.sql"
        
        # Fallback to absolute path relative to this file if simple path fails
        if not os.path.exists(schema_path):
             # c:\...\core\persistence\..\..\db\schema.sql
             root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
             schema_path = os.path.join(root_dir, "db", "schema.sql")

        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as f:
                await self.db.executescript(f.read())
        else:
            await self.db.executescript(SCHEMA_SQL)
        
        # MIGRATION: Add tp_blocked column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN tp_blocked BOOLEAN DEFAULT 0")
            print(f"[REPOS] Migration: added 'tp_blocked' column to 'grid_pairs'")
        except Exception:
            pass

        # MIGRATION: Add group_id column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN group_id INTEGER DEFAULT 0")
            print(f"[REPOS] Migration: added 'group_id' column to 'grid_pairs'")
        except Exception:
            pass
            
        # MIGRATION: Add metadata column to symbol_state if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE symbol_state ADD COLUMN metadata TEXT DEFAULT '{}'")
            print(f"[REPOS] Migration: added 'metadata' column to 'symbol_state'")
        except Exception:
            pass

        # MIGRATION: Add metadata column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN metadata TEXT DEFAULT '{}'")
            print(f"[REPOS] Migration: added 'metadata' column to 'grid_pairs'")
        except Exception:
            pass

        # MIGRATION: Add Grid Bounce state columns to symbol_state if missing
        for sql, label in [
            ("ALTER TABLE symbol_state ADD COLUMN center_price REAL DEFAULT 0.0", "center_price"),
            ("ALTER TABLE symbol_state ADD COLUMN grid_level_1 REAL DEFAULT 0.0", "grid_level_1"),
            ("ALTER TABLE symbol_state ADD COLUMN grid_level_2 REAL DEFAULT 0.0", "grid_level_2"),
            ("ALTER TABLE symbol_state ADD COLUMN active_grid_count INTEGER DEFAULT 1", "active_grid_count"),
            ("ALTER TABLE symbol_state ADD COLUMN position_counter INTEGER DEFAULT 0", "position_counter"),
            ("ALTER TABLE symbol_state ADD COLUMN last_direction TEXT DEFAULT ''", "last_direction"),
        ]:
            try:
                await self.db.execute(sql)
                print(f"[REPOS] Migration: added '{label}' column to 'symbol_state'")
            except Exception:
                pass

        # MIGRATION: Add set-aware ownership columns to ticket_map if missing
        for sql, label in [
            ("ALTER TABLE ticket_map ADD COLUMN set_index INTEGER DEFAULT 0", "set_index"),
            ("ALTER TABLE ticket_map ADD COLUMN grid_level INTEGER DEFAULT 0", "grid_level"),
        ]:
            try:
                await self.db.execute(sql)
                print(f"[REPOS] Migration: added '{label}' column to 'ticket_map'")
            except Exception:
                pass
            
        await self.db.commit()

    async def get_state(self) -> Dict[str, Any]:
        """Load symbol-level state (phase, center_price, cycle_id, anchor_price)."""
        async with self._conn().execute(
            "SELECT * FROM symbol_state WHERE symbol = ?", (self.symbol,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {}

    async def save_state(self, phase: str, center_price: float, iteration: int,
                         cycle_id: int = 0, anchor_price: float = 0.0, metadata: str = '{}'):
        """Upsert symbol state including cycle management fields."""
        await self._conn().execute(
            """
            INSERT INTO symbol_state (symbol, phase, center_price, iteration, last_update_time, cycle_id, anchor_price, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                phase=excluded.phase,
                center_price=excluded.center_price,
                iteration=excluded.iteration,
                last_update_time=excluded.last_update_time,
                cycle_id=excluded.cycle_id,
                anchor_price=excluded.anchor_price,
                metadata=excluded.metadata
            """,
            (self.symbol, phase, center_price, iteration, time.time(), cycle_id, anchor_price, metadata)
        )
        await self._conn().commit()

    async def get_pairs(self) -> List[Dict[str, Any]]:
        """Load all active pairs for this symbol."""
        async with self._conn().execute(
            "SELECT * FROM grid_pairs WHERE symbol = ?", (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def upsert_pair(self, pair_data: Dict[str, Any], metadata: str = '{}'):
        """Insert or Update a single pair (Atomic operation)."""
        # Extract fields from pair_data dict
        await self._conn().execute(
            """
            INSERT INTO grid_pairs (
                symbol, pair_index, buy_price, sell_price, 
                buy_ticket, sell_ticket, buy_filled, sell_filled,
                buy_pending_ticket, sell_pending_ticket,
                trade_count, next_action, is_reopened,
                buy_in_zone, sell_in_zone,
                hedge_ticket, hedge_direction, hedge_active,
                locked_buy_entry, locked_sell_entry, tp_blocked, group_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, pair_index) DO UPDATE SET
                buy_price=excluded.buy_price,
                sell_price=excluded.sell_price,
                buy_ticket=excluded.buy_ticket,
                sell_ticket=excluded.sell_ticket,
                buy_filled=excluded.buy_filled,
                sell_filled=excluded.sell_filled,
                buy_pending_ticket=excluded.buy_pending_ticket,
                sell_pending_ticket=excluded.sell_pending_ticket,
                trade_count=excluded.trade_count,
                next_action=excluded.next_action,
                is_reopened=excluded.is_reopened,
                buy_in_zone=excluded.buy_in_zone,
                sell_in_zone=excluded.sell_in_zone,
                hedge_ticket=excluded.hedge_ticket,
                hedge_direction=excluded.hedge_direction,
                hedge_active=excluded.hedge_active,
                locked_buy_entry=excluded.locked_buy_entry,
                locked_sell_entry=excluded.locked_sell_entry,
                tp_blocked=excluded.tp_blocked,
                group_id=excluded.group_id,
                metadata=excluded.metadata
            """,
            (
                self.symbol, pair_data['index'], pair_data['buy_price'], pair_data['sell_price'],
                pair_data.get('buy_ticket', 0), pair_data.get('sell_ticket', 0),
                pair_data.get('buy_filled', 0), pair_data.get('sell_filled', 0),
                pair_data.get('buy_pending_ticket', 0), pair_data.get('sell_pending_ticket', 0),
                pair_data.get('trade_count', 0), pair_data.get('next_action', 'buy'),
                pair_data.get('is_reopened', 0), pair_data.get('buy_in_zone', 0),
                pair_data.get('sell_in_zone', 0),
                pair_data.get('hedge_ticket', 0),
                pair_data.get('hedge_direction', None),
                pair_data.get('hedge_active', 0),
                pair_data.get('locked_buy_entry', 0.0),
                pair_data.get('locked_sell_entry', 0.0),
                int(pair_data.get('tp_blocked', False)),
                pair_data.get('group_id', 0),
                metadata
            )
        )
        await self._conn().commit()

    async def delete_pair(self, pair_index: int):
        """Remove a pair (used in Leapfrog)."""
        await self._conn().execute(
            "DELETE FROM grid_pairs WHERE symbol = ? AND pair_index = ?",
            (self.symbol, pair_index)
        )
        await self._conn().commit()

    # ========================================================================
    # TICKET MAP (Groups + 3-Cap Strategy)
    # ========================================================================

    async def save_ticket(self, ticket: int, cycle_id: int, pair_index: int,
                          leg: str, trade_count: int = 0,
                          entry_price: float = 0.0, tp_price: float = 0.0, sl_price: float = 0.0,
                          set_index: int = 0, grid_level: int = 0):
        """Save ticket → (pair, leg, prices) mapping for deterministic TP/SL detection."""
        await self._conn().execute(
            """
            INSERT INTO ticket_map (ticket, symbol, cycle_id, pair_index, set_index, grid_level, leg, trade_count, entry_price, tp_price, sl_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket) DO UPDATE SET
                cycle_id=excluded.cycle_id,
                pair_index=excluded.pair_index,
                set_index=excluded.set_index,
                grid_level=excluded.grid_level,
                leg=excluded.leg,
                trade_count=excluded.trade_count,
                entry_price=excluded.entry_price,
                tp_price=excluded.tp_price,
                sl_price=excluded.sl_price
            """,
            (ticket, self.symbol, cycle_id, pair_index, set_index, grid_level, leg, trade_count, entry_price, tp_price, sl_price)
        )
        await self._conn().commit()

    async def get_ticket_map(self) -> Dict[int, Tuple[int, int, int, str, float, float, float]]:
        """Load all ticket mappings for this symbol.

        Returns:
            Dict[ticket, (set_index, pair_index, grid_level, leg, entry_price, tp_price, sl_price)]
        """
        async with self._conn().execute(
            "SELECT ticket, set_index, pair_index, grid_level, leg, entry_price, tp_price, sl_price FROM ticket_map WHERE symbol = ?",
            (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {row['ticket']: (row['set_index'], row['pair_index'], row['grid_level'], row['leg'], row['entry_price'], row['tp_price'], row['sl_price']) for row in rows}

    async def delete_ticket(self, ticket: int):
        """Remove a ticket from the map (on position close)."""
        await self._conn().execute(
            "DELETE FROM ticket_map WHERE ticket = ?",
            (ticket,)
        )
        await self._conn().commit()

    async def clear_ticket_map(self):
        """Clear all tickets for this symbol (on fresh start)."""
        await self._conn().execute(
            "DELETE FROM ticket_map WHERE symbol = ?",
            (self.symbol,)
        )
        await self._conn().commit()

    # ========================================================================
    # TRADE HISTORY
    # ========================================================================

    async def log_trade(self, event: Dict[str, Any]):
        """Log a trade event to history table (Permanent storage)."""
        await self._conn().execute(
            """
            INSERT INTO trade_history (symbol, timestamp, event_type, pair_index, direction, price, lot_size, ticket, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.symbol, event['timestamp'], event['event_type'], 
                event['pair_index'], event['direction'], event['price'], 
                event['lot_size'], event['ticket'], event.get('notes', '')
            )
        )
        await self._conn().commit()

    # ========================================================================
    # QUEUED CLOSE STRATEGY (agent/02 §9)
    # ========================================================================

    async def save_moving_position(self, ticket: int, cycle_id: int, entry: float,
                                   tp_price: float, sl_price: float,
                                   direction: str, slot_index: int, closed: bool = False):
        await self._conn().execute(
            """
            INSERT INTO moving_positions (ticket, symbol, cycle_id, entry, tp_price, sl_price, direction, slot_index, closed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket) DO UPDATE SET
                cycle_id=excluded.cycle_id,
                entry=excluded.entry,
                tp_price=excluded.tp_price,
                sl_price=excluded.sl_price,
                direction=excluded.direction,
                slot_index=excluded.slot_index,
                closed=excluded.closed
            """,
            (ticket, self.symbol, cycle_id, entry, tp_price, sl_price, direction, slot_index, int(closed))
        )
        await self._conn().commit()

    async def get_moving_positions(self, open_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM moving_positions WHERE symbol = ?"
        if open_only:
            sql += " AND closed = 0"
        async with self._conn().execute(sql, (self.symbol,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def mark_moving_position_closed(self, ticket: int):
        await self._conn().execute(
            "UPDATE moving_positions SET closed = 1 WHERE ticket = ?",
            (ticket,)
        )
        await self._conn().commit()

    async def save_constant_targets(self, cycle_id: int, targets: List[Dict[str, Any]]):
        """Bulk-insert precomputed constant targets for a cycle.
        Each dict: {direction, slot_index, price, fired}.
        """
        await self._conn().execute(
            "DELETE FROM constant_targets WHERE symbol = ? AND cycle_id = ?",
            (self.symbol, cycle_id)
        )
        for t in targets:
            await self._conn().execute(
                """
                INSERT INTO constant_targets (symbol, cycle_id, direction, slot_index, price, fired)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, cycle_id, direction, slot_index) DO UPDATE SET
                    price=excluded.price,
                    fired=excluded.fired
                """,
                (self.symbol, cycle_id, t['direction'], t['slot_index'], t['price'], int(t.get('fired', False)))
            )
        await self._conn().commit()

    async def get_constant_targets(self, cycle_id: int) -> List[Dict[str, Any]]:
        async with self._conn().execute(
            "SELECT * FROM constant_targets WHERE symbol = ? AND cycle_id = ? ORDER BY direction, slot_index",
            (self.symbol, cycle_id)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def mark_constant_target_fired(self, cycle_id: int, direction: str, slot_index: int):
        await self._conn().execute(
            "UPDATE constant_targets SET fired = 1 WHERE symbol = ? AND cycle_id = ? AND direction = ? AND slot_index = ?",
            (self.symbol, cycle_id, direction, slot_index)
        )
        await self._conn().commit()

    async def enqueue_constant_close(self, cycle_id: int, direction: str,
                                     slot_index: int, retry_count: int, enqueued_at: float):
        await self._conn().execute(
            """
            INSERT INTO constant_queue (symbol, cycle_id, direction, slot_index, retry_count, enqueued_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self.symbol, cycle_id, direction, slot_index, retry_count, enqueued_at)
        )
        await self._conn().commit()

    async def update_constant_queue_retry(self, row_id: int, retry_count: int):
        await self._conn().execute(
            "UPDATE constant_queue SET retry_count = ? WHERE id = ?",
            (retry_count, row_id)
        )
        await self._conn().commit()

    async def delete_constant_queue_entry(self, cycle_id: int, direction: str, slot_index: int):
        """Delete the queue row for a successfully-released (cycle, direction, slot)."""
        await self._conn().execute(
            "DELETE FROM constant_queue WHERE symbol = ? AND cycle_id = ? AND direction = ? AND slot_index = ?",
            (self.symbol, cycle_id, direction, slot_index)
        )
        await self._conn().commit()

    async def bump_constant_queue_retry(self, cycle_id: int, direction: str,
                                        slot_index: int, retry_count: int):
        """Persist an incremented retry count for a pending queue row."""
        await self._conn().execute(
            """UPDATE constant_queue SET retry_count = ?
               WHERE symbol = ? AND cycle_id = ? AND direction = ? AND slot_index = ?""",
            (retry_count, self.symbol, cycle_id, direction, slot_index)
        )
        await self._conn().commit()

    async def get_constant_queue(self) -> List[Dict[str, Any]]:
        async with self._conn().execute(
            "SELECT * FROM constant_queue WHERE symbol = ? ORDER BY id",
            (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def clear_constant_queue(self, cycle_id: Optional[int] = None):
        """Clear this symbol's queue rows; optionally scope to one cycle.
        Must be called explicitly at cycle end — stale rows with mismatched
        cycle_id would trip a later reconciliation pass (agent/04).
        """
        if cycle_id is None:
            await self._conn().execute(
                "DELETE FROM constant_queue WHERE symbol = ?", (self.symbol,)
            )
        else:
            await self._conn().execute(
                "DELETE FROM constant_queue WHERE symbol = ? AND cycle_id = ?",
                (self.symbol, cycle_id)
            )
        await self._conn().commit()

    async def save_constant_ticket(self, ticket: int, cycle_id: int):
        await self._conn().execute(
            "INSERT INTO constant_tickets (ticket, symbol, cycle_id) VALUES (?, ?, ?)",
            (ticket, self.symbol, cycle_id)
        )
        await self._conn().commit()

    async def get_constant_tickets(self) -> List[int]:
        async with self._conn().execute(
            "SELECT ticket FROM constant_tickets WHERE symbol = ?",
            (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [r['ticket'] for r in rows]

    async def delete_constant_ticket(self, ticket: int):
        await self._conn().execute(
            "DELETE FROM constant_tickets WHERE ticket = ?",
            (ticket,)
        )
        await self._conn().commit()

    async def clear_constant_tickets(self):
        await self._conn().execute(
            "DELETE FROM constant_tickets WHERE symbol = ?",
            (self.symbol,)
        )
        await self._conn().commit()

    async def clear_moving_positions(self):
        await self._conn().execute(
            "DELETE FROM moving_positions WHERE symbol = ?",
            (self.symbol,)
        )
        await self._conn().commit()

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None
