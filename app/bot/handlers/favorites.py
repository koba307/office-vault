from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.favorites.service import add_favorite, list_favorites

router = Router()


@router.callback_query(F.data.startswith("favorite:add:"))
async def add_fav(callback: CallbackQuery) -> None:
    rid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from uuid import UUID
        await add_favorite(session, user.id, UUID(rid))
    await callback.answer("Добавлено в избранное")


@router.callback_query(F.data == "favorites:list")
async def list_fav(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        favs = await list_favorites(session, user.id)

    if not favs:
        text = "⭐ Избранное пусто"
    else:
        text = "⭐ Избранное:\n" + "\n".join(f"• {r.title}" for r in favs)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")]]))
    await callback.answer()
