from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers.menu import get_main_menu_keyboard
from app.core.config import settings
from app.database.models import Record, RecordType
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active, user_can_access_category
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = Router()


@router.callback_query(lambda c: c.data and c.data.startswith("type:"))
async def type_callback(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Некорректный запрос")
        return

    rec_type = parts[1].upper()
    category = parts[2]

    # TODO: получить сессию и проверить права
    # Пока показываем заглушку со списком (будет реализовано через сервис)
    title_map = {
        "CREDENTIALS": "🔐 Логины и пароли",
        "SIP": "☎️ SIP и телефония",
        "LINKS": "🔗 Ссылки",
        "SETTINGS": "⚙️ Настройки",
        "INSTRUCTIONS": "📚 Инструкции",
        "FILES": "📁 Файлы и документы",
        "OTHER": "🗂 Прочее",
    }
    title = title_map.get(rec_type, rec_type)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить запись", callback_data=f"record:add:{rec_type}:{category}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"menu:category:{category}")],
        ]
    )
    await callback.message.edit_text(
        f"{title}\n\nРаздел: {category}\n\n(Список записей будет загружен после реализации сервиса записей)",
        reply_markup=kb,
    )
    await callback.answer()
