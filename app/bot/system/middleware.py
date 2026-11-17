from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from .confirmation import ConfirmationManager
from .message_lifecycle import MessageLifecycleManager
from .navigation import GlobalNavigation, NavigationManager
from .process import ProcessManager
from .progress import ProgressManager

logger = logging.getLogger(__name__)


class SystemMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self.navigation = NavigationManager()
        self.confirmation = ConfirmationManager()
        self.process = ProcessManager()
        self.progress = ProgressManager()
        self.lifecycle = MessageLifecycleManager()

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        from aiogram.types import Message
        if isinstance(event, CallbackQuery):
            data_str = event.data or ""
            skip = data_str.startswith(
                ("menu:main", "menu:back", "menu:cancel", "reg:", "rbac:",
                 "admin:toggle", "admin:setrole", "admin:newrole", "admin:togglegroup",
                 "max:get", "max:upload")
            )
            if not skip and event.from_user:
                chat_id = event.message.chat.id if event.message else event.from_user.id
                GlobalNavigation.push(event.from_user.id, chat_id, data_str)
        elif isinstance(event, Message):
            logger.info("SYSTEM message: %r from %s", event.text, event.from_user.id if event.from_user else None)
        print("MIDDLEWARE CALLED", type(event).__name__, getattr(event, "data", None), flush=True)
        data["system"] = self
        data["navigation"] = self.navigation
        data["confirmation"] = self.confirmation
        data["process"] = self.process
        data["progress"] = self.progress
        data["lifecycle"] = self.lifecycle
        return await handler(event, data)
