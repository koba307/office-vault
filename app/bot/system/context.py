from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class OperationStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


@dataclass
class OperationContext:
    operation_id: UUID = field(default_factory=uuid4)
    user_id: int | None = None
    chat_id: int | None = None
    operation_type: str = "generic"
    status: OperationStatus = OperationStatus.PENDING
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    stage: str = "started"
    progress: dict[str, Any] = field(default_factory=dict)
    cancellation_event: asyncio.Event | None = None
    temp_message_id: int | None = None
    keyboard_message_id: int | None = None
    error: str | None = None
    completed_at: datetime | None = None

    def __post_init__(self):
        import asyncio

        if self.cancellation_event is None:
            self.cancellation_event = asyncio.Event()
