from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest


class MessageLifecycleManager:
    def __init__(self) -> None:
        self._policies: dict[int, str] = {}

    def register(self, message_id: int, policy: str = "DELETE_ON_CALLBACK") -> None:
        self._policies[message_id] = policy

    async def delete(self, bot: Bot, chat_id: int, message_id: int) -> bool:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            self._policies.pop(message_id, None)
            return True
        except TelegramBadRequest:
            self._policies.pop(message_id, None)
            return False
        except Exception:  # noqa: BLE001
            return False

    async def remove_keyboard(self, bot: Bot, chat_id: int, message_id: int) -> bool:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            return True
        except TelegramBadRequest:
            return False
        except Exception:  # noqa: BLE001
            return False

    async def cleanup_on_callback(self, bot: Bot, chat_id: int, message_id: int, policy: str | None = None) -> None:
        policy = policy or self._policies.get(message_id, "EDIT_KEYBOARD_ON_CALLBACK")
        if policy == "DELETE_ON_CALLBACK":
            await self.delete(bot, chat_id, message_id)
        elif policy in ("EDIT_KEYBOARD_ON_CALLBACK", "DELETE_AFTER_OPERATION"):
            await self.remove_keyboard(bot, chat_id, message_id)
