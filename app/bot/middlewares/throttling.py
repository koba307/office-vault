import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

RATE_LIMIT = 30
RATE_WINDOW = 60  # секунд

_user_request_times: dict[int, list[float]] = defaultdict(list)


class ThrottlingMiddleware(BaseMiddleware):
    """Ограничение частоты запросов (rate limiting)."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)) and event.from_user:
            telegram_id = event.from_user.id

        if telegram_id is None:
            return await handler(event, data)

        now = time.time()
        times = _user_request_times[telegram_id]
        _user_request_times[telegram_id] = [t for t in times if now - t < RATE_WINDOW]

        if len(_user_request_times[telegram_id]) >= RATE_LIMIT:
            if isinstance(event, Message):
                await event.answer("Слишком много запросов. Попробуйте позже.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Слишком много запросов", show_alert=True)
            return None

        _user_request_times[telegram_id].append(now)
        return await handler(event, data)