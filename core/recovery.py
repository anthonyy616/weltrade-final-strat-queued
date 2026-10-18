"""Durable lifecycle state for MT5 disconnect recovery."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


RECOVERY_FILE = "recovery_state.json"
RECOVERY_FAILED = "RECOVERY_FAILED"
MAX_RECOVERY_ATTEMPTS = 9
RECOVERY_WINDOW_SECONDS = 3 * 60 * 60
RECOVERY_ATTEMPTS_PER_HOUR = 3


@dataclass
class RecoveryCheckpoint:
    version: int = 1
    user_id: str = ""
    session_id: str = ""
    lifecycle: str = "STOPPED"
    session_started_at: float = 0.0
    remaining_seconds: Optional[float] = None
    last_heartbeat_at: float = 0.0
    disconnect_started_at: Optional[float] = None
    reconnected_at: Optional[float] = None
    recovery_attempts: int = 0
    recovery_window_started_at: Optional[float] = None
    graceful_stop: bool = False
    timeout_graceful_stop_triggered: bool = False
    hard_stop_remaining_seconds: Optional[float] = None
    last_reconciliation: Optional[Dict[str, Any]] = None


class RecoveryStore:
    """Atomically persists the process owner's recovery checkpoint."""

    def __init__(self, path: str = RECOVERY_FILE):
        self.path = Path(path)
        self.checkpoint = self._load()

    def _load(self) -> RecoveryCheckpoint:
        if not self.path.exists():
            return RecoveryCheckpoint()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            fields = {field.name for field in RecoveryCheckpoint.__dataclass_fields__.values()}
            return RecoveryCheckpoint(**{key: value for key, value in data.items()
                                         if key in fields})
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Unable to load recovery checkpoint {self.path}: {exc}") from exc

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=str(self.path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(self.checkpoint), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise RuntimeError(f"Unable to save recovery checkpoint {self.path}: {exc}") from exc

    def begin_session(self, user_id: str, remaining_seconds: Optional[float]) -> RecoveryCheckpoint:
        self.checkpoint = RecoveryCheckpoint(
            user_id=user_id,
            session_id=uuid.uuid4().hex,
            lifecycle="RUNNING",
            session_started_at=time.time(),
            remaining_seconds=remaining_seconds,
            last_heartbeat_at=time.time(),
        )
        self.save()
        return self.checkpoint

    def heartbeat(self, *, remaining_seconds: Optional[float] = None,
                  lifecycle: Optional[str] = None) -> None:
        if remaining_seconds is not None:
            self.checkpoint.remaining_seconds = max(0.0, remaining_seconds)
        if lifecycle is not None:
            self.checkpoint.lifecycle = lifecycle
        self.checkpoint.last_heartbeat_at = time.time()
        self.save()

    def mark_disconnect(self) -> None:
        if self.checkpoint.disconnect_started_at is None:
            self.checkpoint.disconnect_started_at = time.time()
        self.checkpoint.lifecycle = "DISCONNECTED"
        self.save()

    def begin_reconnect_attempt(self) -> bool:
        now = time.time()
        window_start = self.checkpoint.recovery_window_started_at
        if window_start is None or now - window_start >= RECOVERY_WINDOW_SECONDS:
            self.checkpoint.recovery_window_started_at = now
            self.checkpoint.recovery_attempts = 0
        if self.checkpoint.recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
            self.checkpoint.lifecycle = RECOVERY_FAILED
            self.save()
            return False
        self.checkpoint.recovery_attempts += 1
        self.checkpoint.lifecycle = "RECONNECTING"
        self.save()
        return True

    def mark_reconnected(self, summary: Optional[Dict[str, Any]] = None) -> None:
        self.checkpoint.lifecycle = "RECONCILING"
        self.checkpoint.reconnected_at = time.time()
        self.checkpoint.disconnect_started_at = None
        self.checkpoint.last_reconciliation = summary
        self.save()

    def mark_recovery_failed(self) -> None:
        self.checkpoint.lifecycle = RECOVERY_FAILED
        self.save()

    def mark_stopped(self, *, graceful_stop: bool = False,
                     timeout: bool = False) -> None:
        self.checkpoint.lifecycle = "STOPPED"
        self.checkpoint.graceful_stop = graceful_stop
        self.checkpoint.timeout_graceful_stop_triggered = timeout
        self.save()
