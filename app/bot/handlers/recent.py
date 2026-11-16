"""Экран «Недавние просмотры» (Phase 07).

Список отдаётся с пагинацией recent:page:{n}. В строках показываются только
название, категория, тип и дата просмотра: значения полей и секреты здесь
не отображаются и не сохраняются. Устаревшие кнопки (побитая страница или
страница вне истории) получают STALE_TEXT и показываются корректно.
"""

import html
from datetime import datetime
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers.menu import STALE_TEXT, TYPE_TITLES
from app.bot.keyboards.layout import smart_layout
from app.bot.keyboards.pagination import confirm_keyboard, pagination_keyboard, total_pages
from app.database.models import RecordType
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.recent.service import RecentEntry, clear_recent, list_recent

router = Router()

LIST_CALLBACK = "recent:list"
PAGE_PREFIX = "recent:page:"
CLEAR_CALLBACK = "recent:clear"
CLEAR_YES_CALLBACK = "recent:clear:yes"
CLEAR_NO_CALLBACK = "recent:clear:no"
PAGE_SIZE = 10


def parse_page_callback(data: str | None) -> int | None:
    """Номер страницы из recent:page:{n}; None — битая или устаревшая кнопка."""
    if not data or not data.startswith(PAGE_PREFIX):
        return None
    raw = data.removeprefix(PAGE_PREFIX)
    if not raw.isdigit():
        return None
    return int(raw)


def _type_title(record_type: str) -> str:
    """Человекочитаемое название типа записи (как в меню и поиске)."""
    try:
        return TYPE_TITLES[RecordType(record_type)]
    except (ValueError, KeyError):
        return record_type


def _format_viewed_at(viewed_at: datetime | None) -> str:
    return viewed_at.strftime("%d.%m.%Y %H:%M") if viewed_at else "—"


def format_recent_text(entries: list[RecentEntry], page: int, pages: int) -> str:
    """Текст экрана: название, категория, тип и дата просмотра каждой записи."""
    lines = ["🕘 <b>Недавние просмотры</b>", ""]
    if not entries:
        lines.append("История пока пуста.")
    for entry in entries:
        lines.append(
            f"• <b>{html.escape(entry.title)}</b>\n"
            f"   {html.escape(entry.category_name)} · {_type_title(entry.record_type)} · "
            f"👁 {_format_viewed_at(entry.viewed_at)}"
        )
    if pages > 1:
        lines += ["", f"Страница {page + 1} из {pages}"]
    return "\n".join(lines)


def build_recent_rows(
    entries: list[RecentEntry], total: int, page: int
) -> list[list[InlineKeyboardButton]]:
    """Кнопки: просмотры, пагинация, очистка истории и возврат в меню."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📄 {entry.title}", callback_data=f"record:view:{entry.record_id}"
            )
        ]
        for entry in entries
    ]
    rows.extend(pagination_keyboard(total, page, PAGE_SIZE, PAGE_PREFIX).inline_keyboard)
    if total:
        rows.append(
            [InlineKeyboardButton(text="🧹 Очистить историю", callback_data=CLEAR_CALLBACK)]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return rows


async def _active_user_id(callback: CallbackQuery) -> UUID | None:
    """Возвращает id активного пользователя; при отказе отвечает алертом."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if await is_user_active(user):
            return user.id
    await callback.answer("Нет доступа", show_alert=True)
    return None


async def _render_page(callback: CallbackQuery, user_id: UUID, page: int) -> str | None:
    """Рисует страницу истории. Возвращает текст алерта, если кнопка устарела."""
    async with async_session() as session:
        entries, total = await list_recent(
            session, user_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE
        )

    pages = total_pages(total, PAGE_SIZE)
    stale = False
    if total and not entries:
        # Кнопка ведёт за границу истории (записи скрылись после смены прав) —
        # показываем последнюю доступную страницу и предупреждаем
        page = pages - 1
        stale = True
        async with async_session() as session:
            entries, total = await list_recent(
                session, user_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE
            )

    text = format_recent_text(entries, page, pages)
    rows = build_recent_rows(entries, total, page)
    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(rows)),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        # Повторное нажатие на ту же страницу не считается ошибкой
        if "message is not modified" not in str(exc):
            raise
    return STALE_TEXT if stale else None


@router.callback_query(F.data == LIST_CALLBACK)
async def recent_list(callback: CallbackQuery) -> None:
    """recent:list — первая страница недавних просмотров."""
    user_id = await _active_user_id(callback)
    if user_id is None:
        return
    await _render_page(callback, user_id, 0)
    await callback.answer()


@router.callback_query(F.data.startswith(PAGE_PREFIX))
async def recent_page(callback: CallbackQuery) -> None:
    """recent:page:{n} — пагинация истории; побитые кнопки считаются устаревшими."""
    page = parse_page_callback(callback.data)
    if page is None:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    user_id = await _active_user_id(callback)
    if user_id is None:
        return
    note = await _render_page(callback, user_id, page)
    if note:
        await callback.answer(note, show_alert=True)
    else:
        await callback.answer()


@router.callback_query(F.data == CLEAR_CALLBACK)
async def recent_clear_ask(callback: CallbackQuery) -> None:
    """Запрашивает подтверждение: очистка истории необратима."""
    if await _active_user_id(callback) is None:
        return
    await callback.message.edit_text(
        "🧹 Очистить всю историю просмотров?",
        reply_markup=confirm_keyboard(CLEAR_YES_CALLBACK, CLEAR_NO_CALLBACK),
    )
    await callback.answer()


@router.callback_query(F.data == CLEAR_YES_CALLBACK)
async def recent_clear_yes(callback: CallbackQuery) -> None:
    user_id = await _active_user_id(callback)
    if user_id is None:
        return
    async with async_session() as session:
        removed = await clear_recent(session, user_id)
    await _render_page(callback, user_id, 0)
    await callback.answer("История очищена" if removed else "История уже пуста")


@router.callback_query(F.data == CLEAR_NO_CALLBACK)
async def recent_clear_no(callback: CallbackQuery) -> None:
    """Отмена очистки: возвращаемся к списку без изменений."""
    user_id = await _active_user_id(callback)
    if user_id is None:
        return
    await _render_page(callback, user_id, 0)
    await callback.answer("Очистка отменена")
