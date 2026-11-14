from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.handlers.menu import get_main_menu_keyboard
from app.core.config import settings

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    name = message.from_user.full_name or "сотрудник"
    is_superadmin = user_id == settings.superadmin_telegram_id

    if is_superadmin:
        text = (
            f"🏢 <b>OFFICE VAULT</b>\n\n"
            f"Добро пожаловать, {name}.\n"
            f"Роль: <b>SUPERADMIN</b>\n\n"
            f"Выберите рабочий раздел:"
        )
        kb = get_main_menu_keyboard(is_admin=True)
    else:
        text = (
            f"🏢 <b>OFFICE VAULT</b>\n\n"
            f"Добро пожаловать, {name}.\n"
            f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
            f"Доступ предоставляется только зарегистрированным сотрудникам.\n"
            f"Обратитесь к администратору для добавления в систему."
        )
        kb = None

    await message.answer(text, reply_markup=kb)
