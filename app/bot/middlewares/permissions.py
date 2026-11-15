from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update


class PermissionMiddleware(BaseMiddleware):
    """Проверка прав на каждый запрос (Default Deny)."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        is_active = data.get("is_active", False)

        # Пропускаем /start для неизвестных пользователей (инвайты обрабатываются отдельно)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        if user is None or not is_active:
            if isinstance(event, Message):
                await event.answer("Доступ предоставляется только зарегистрированным сотрудникам.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return None

        # Проверка прав делегируется хендлерам через PermissionService
        # (Default Deny — если хендлер не проверил, middleware блокирует)
        data["require_permission"] = True
        return await handler(event, data)