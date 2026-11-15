import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

from app.database.models import AuditLog
from app.database.session import async_session

logger = logging.getLogger(__name__)


class AuditMiddleware(BaseMiddleware):
    """Аудит всех действий пользователя."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)) and event.from_user:
            telegram_id = event.from_user.id

        action = None
        object_type = None
        object_id = None

        if isinstance(event, CallbackQuery) and event.data:
            parts = event.data.split(":")
            action = parts[0]
            object_type = parts[1] if len(parts) > 1 else None
            object_id = parts[-1] if len(parts) > 2 else None
        elif isinstance(event, Message) and event.text:
            action = "message"
            object_type = "command" if event.text.startswith("/") else "text"

        result = await handler(event, data)

        if action and action not in ("message", "menu", "start"):
            try:
                async with async_session() as session:
                    log = AuditLog(
                        actor_user_id=user.id if user else None,
                        action=action,
                        object_type=object_type or "unknown",
                        object_id=object_id,
                        result="SUCCESS",
                        correlation_id=str(uuid.uuid4()),
                        telegram_id=telegram_id,
                        role=user.role.value if user else None,
                    )
                    session.add(log)
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Audit write failed: %s", exc)

        return result