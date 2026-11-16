import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """Резолв пользователя по Telegram ID, проверка статуса, инъекция в data."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)) and event.from_user:
            telegram_id = event.from_user.id
            if isinstance(event, CallbackQuery):
                logger.info("callback received: %s from %s", event.data, telegram_id)

        if telegram_id is None:
            return await handler(event, data)
        print("AUTH MIDDLEWARE CALLED", telegram_id, type(event).__name__, getattr(event, "data", None), flush=True)

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, telegram_id)
            if user is not None:
                user.last_seen_at = datetime.now(UTC)
                await session.commit()
            data["user"] = user
            data["session"] = session
            data["is_active"] = user is not None and user.status == "ACTIVE"

        return await handler(event, data)