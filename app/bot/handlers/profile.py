from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active

router = Router()


@router.callback_query(F.data == "profile:view")
async def profile_view(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        groups = []
        if user:
            from app.services.auth.admin import get_user_groups

            groups = await get_user_groups(session, user.id)

    group_names = ", ".join(g.name for g in groups) or "нет"
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"Имя: {user.display_name or user.username or user.telegram_id}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Роль: {user.role.value}\n"
        f"Статус: {user.status.value}\n"
        f"Группы: {group_names}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
