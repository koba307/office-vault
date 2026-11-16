import html
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from app.bot.keyboards.layout import smart_layout
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers.menu import PAGE_SIZE, TYPE_TITLES
from app.bot.handlers.records import render_record_card
from app.bot.keyboards.pagination import pagination_keyboard, total_pages
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.favorites.service import (
    FavoriteError,
    FavoriteItem,
    add_favorite,
    list_favorites,
    remove_favorite,
)

router = Router()

STALE_TEXT = "Кнопка устарела. Откройте избранное заново."
FAVORITES_PAGE_PREFIX = "favorites:page"


def _parse_uuid(value: str | None) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _escape(value) -> str:
    return html.escape(str(value), quote=False)


def _clip(value, limit: int = 40) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def favorites_view(
    items: list[FavoriteItem], total: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура страницы избранного."""
    if not items and total == 0:
        return "⭐ Избранное пусто", InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")]]
        )

    pages = total_pages(total, PAGE_SIZE)
    header = f"⭐ <b>Избранное</b> · всего: {total}"
    if pages > 1:
        header += f" · стр. {page + 1}/{pages}"
    lines = [header, ""]
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(items, start=page * PAGE_SIZE + 1):
        record = item.record
        added = item.added_at.strftime("%d.%m.%Y") if item.added_at else "—"
        type_title = TYPE_TITLES.get(record.record_type, str(record.record_type))
        lines.append(f"{index}. <b>{_escape(record.title)}</b>")
        lines.append(
            f"   Раздел: {_escape(item.category_name)} · Тип: {_escape(type_title)} · "
            f"добавлено: {added}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {_clip(record.title)}",
                    callback_data=f"record:view:{record.id}",
                )
            ]
        )
    if pages > 1:
        nav = pagination_keyboard(total, page, PAGE_SIZE, FAVORITES_PAGE_PREFIX)
        rows.extend(nav.inline_keyboard)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))


async def _show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")


async def _render_favorites(callback: CallbackQuery, page: int) -> None:
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        items, total = await list_favorites(
            session, user.id, limit=PAGE_SIZE, offset=page * PAGE_SIZE
        )
        pages = total_pages(total, PAGE_SIZE)
        # Запись могли убрать или отозвать доступ: переходим на последнюю доступную страницу.
        if page >= pages:
            page = pages - 1
            items, total = await list_favorites(
                session, user.id, limit=PAGE_SIZE, offset=page * PAGE_SIZE
            )

    text, markup = favorites_view(items, total, page)
    await _show(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("favorite:add:"))
async def add_fav(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            added = await add_favorite(session, user.id, rid)
        except FavoriteError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    await render_record_card(
        callback, rid, notice="Добавлено в избранное" if added else "Уже в избранном"
    )


@router.callback_query(F.data.startswith("favorite:remove:"))
async def remove_fav(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        removed = await remove_favorite(session, user.id, rid)
    await render_record_card(
        callback, rid, notice="Убрано из избранного" if removed else "Записи не было в избранном"
    )


@router.callback_query(F.data == "favorites:list")
async def list_fav(callback: CallbackQuery) -> None:
    await _render_favorites(callback, page=0)


@router.callback_query(F.data.startswith(f"{FAVORITES_PAGE_PREFIX}:"))
async def list_fav_page(callback: CallbackQuery) -> None:
    raw = (callback.data or "").split(":")[-1]
    try:
        page = int(raw)
    except ValueError:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    if page < 0:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    await _render_favorites(callback, page=page)


__all__ = ["router"]
