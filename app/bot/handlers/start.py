from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.handlers.menu import get_main_menu_keyboard
from app.core.config import settings
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    name = message.from_user.full_name or "сотрудник"

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, user_id)

    is_superadmin = user_id == settings.superadmin_telegram_id or (user and user.role.value == "SUPERADMIN")

    if user and is_user_active(user):
        role_text = user.role.value if user else "EMPLOYEE"
        text = (
            f"🏢 <b>OFFICE VAULT</b>\n\n"
            f"Добро пожаловать, {name}.\n"
            f"Роль: <b>{role_text}</b>\n\n"
            f"Выберите рабочий раздел:"
        )
        kb = await get_main_menu_keyboard(user=user, is_admin=is_superadmin)
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
