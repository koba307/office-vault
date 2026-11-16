from __future__ import annotations

import asyncio
from typing import Any

from .context import OperationContext, OperationStatus


class ProcessManager:
    def __init__(self) -> None:
        self._ops: dict[str, OperationContext] = {}

    def register(self, user_id: int, chat_id: int, operation_type: str) -> OperationContext:
        ctx = OperationContext(user_id=user_id, chat_id=chat_id, operation_type=operation_type)
        self._ops[str(ctx.operation_id)] = ctx
        return ctx

    def get(self, operation_id: str) -> OperationContext | None:
        return self._ops.get(operation_id)

    def active_for_user(self, user_id: int) -> list[OperationContext]:
        return [c for c in self._ops.values() if c.user_id == user_id and c.status in (OperationStatus.PENDING, OperationStatus.RUNNING, OperationStatus.CANCELLATION_REQUESTED)]

    def request_cancel(self, operation_id: str, user_id: int) -> bool:
        ctx = self.get(operation_id)
        if ctx is None or ctx.user_id != user_id:
            return False
        if ctx.status in (OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.CANCELLED, OperationStatus.TIMEOUT):
            return False
        ctx.status = OperationStatus.CANCELLATION_REQUESTED
        if ctx.cancellation_event is not None:
            ctx.cancellation_event.set()
        return True

    def is_cancelled(self, ctx: OperationContext) -> bool:
        return ctx.status == OperationStatus.CANCELLATION_REQUESTED or (ctx.cancellation_event is not None and ctx.cancellation_event.is_set())

    def update_status(self, ctx: OperationContext, status: OperationStatus, stage: str | None = None, progress: dict[str, Any] | None = None, error: str | None = None) -> None:
        ctx.status = status
        if stage is not None:
            ctx.stage = stage
        if progress is not None:
            ctx.progress.update(progress)
        if error is not None:
            ctx.error = error
        if status in (OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.CANCELLED, OperationStatus.TIMEOUT):
            ctx.completed_at = __import__("datetime").datetime.now(__import__("datetime").UTC)

    def complete(self, ctx: OperationContext, progress: dict[str, Any] | None = None) -> None:
        self.update_status(ctx, OperationStatus.COMPLETED, stage="completed", progress=progress)

    def fail(self, ctx: OperationContext, error: str) -> None:
        self.update_status(ctx, OperationStatus.FAILED, stage="failed", error=error)

    def cleanup(self, operation_id: str) -> None:
        self._ops.pop(operation_id, None)
