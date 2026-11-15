from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.recent.service import list_recent_records

router = Router()


@router.callback_query(F.data == "recent:list")
async def recent_list(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        records = await list_recent_records(session, user)

    if not records:
        text = "🕘 Недавних записей нет"
        buttons = []
    else:
        text = "🕘 Недавние записи:"
        buttons = [[InlineKeyboardButton(text=f"📄 {r.title}", callback_data=f"record:view:{r.id}")] for r in records]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()
