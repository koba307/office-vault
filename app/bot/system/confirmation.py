from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

CONFIRM_TTL = 300


@dataclass
class PendingConfirmation:
    token: str
    user_id: int
    chat_id: int
    action: str
    description: str
    severity: str
    params: dict[str, Any]
    callback: Callable[[], Awaitable[Any]]
    created_at: float = field(default_factory=time.time)
    used: bool = False

    def expired(self) -> bool:
        return time.time() - self.created_at > CONFIRM_TTL


class ConfirmationManager:
    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}

    def request(
        self,
        *,
        user_id: int,
        chat_id: int,
        action: str,
        description: str,
        callback: Callable[[], Awaitable[Any]],
        severity: str = "standard",
        params: dict[str, Any] | None = None,
        typed: str | None = None,
    ) -> PendingConfirmation:
        token = uuid.uuid4().hex
        item = PendingConfirmation(
            token=token,
            user_id=user_id,
            chat_id=chat_id,
            action=action,
            description=description,
            severity=severity,
            params=params or {},
            callback=callback,
        )
        if typed:
            item.params["typed"] = typed
        self._pending[token] = item
        # prune expired
        self._prune()
        return item

    def _prune(self) -> None:
        for token, item in list(self._pending.items()):
            if item.expired() or item.used:
                del self._pending[token]

    def get(self, token: str, user_id: int) -> PendingConfirmation | None:
        item = self._pending.get(token)
        if item is None or item.expired() or item.used or item.user_id != user_id:
            return None
        return item

    async def confirm(self, token: str, user_id: int, typed_value: str | None = None) -> Any:
        item = self.get(token, user_id)
        if item is None:
            raise ValueError("Подтверждение недоступно или истекло.")
        typed = item.params.get("typed")
        if typed and typed_value != typed:
            raise ValueError("Текст подтверждения не совпадает.")
        item.used = True
        result = await item.callback()
        del self._pending[token]
        return result

    def cancel(self, token: str, user_id: int) -> bool:
        item = self.get(token, user_id)
        if item is None:
            return False
        item.used = True
        del self._pending[token]
        return True
