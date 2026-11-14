from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.core.config import settings

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    name = message.from_user.full_name or "сотрудник"

    if user_id == settings.superadmin_telegram_id:
        text = (
            f"🏢 <b>OFFICE VAULT</b>\n\n"
            f"Добро пожаловать, {name}.\n"
            f"Вы — SUPERADMIN.\n\n"
            f"Выберите действие:"
        )
    else:
        text = (
            f"🏢 <b>OFFICE VAULT</b>\n\n"
            f"Добро пожаловать, {name}.\n"
            f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
            f"Доступ предоставляется только зарегистрированным сотрудникам.\n"
            f"Обратитесь к администратору для добавления в систему."
        )

    await message.answer(text)
