"""Экраны архива (Phase 05): список скрытых записей, фильтры, восстановление и окончательное удаление.

Роутер подключается в main.py раньше menu.router: кнопка «🗃 Архив» главного меню
ведёт на полноценный экран Phase 05 вместо упрощённого списка Phase 04.
Формат callback-адресов списка: archive:l:{status}:{category}:{type}:{page},
где status — a (архив) или d (корзина).
"""

import html
import re
from uuid import UUID

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from app.bot.handlers.menu import PAGE_SIZE, STALE_TEXT, breadcrumbs, category_emoji
from app.bot.keyboards.pagination import confirm_keyboard, pagination_keyboard
from app.database.models import Category, Record, RecordType, User
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active, log_audit
from app.services.permissions import PermissionService
from app.services.records.service import (
    PERMANENTLY_DELETABLE_STATUSES,
    RecordError,
    permanent_delete,
    restore_record,
)

router = Router()

ALL_FILTER = "ALL"
# Статусы экрана архива: короткая буква вместо длинного значения статуса.
STATUS_CODES = {"a": "ARCHIVED", "d": "DELETED"}
STATUS_TITLES = {"a": "🗄 Архив", "d": "🗑 Корзина"}

# Максимум Telegram для callback_data (в байтах); проверяется в тестах.
CALLBACK_LIMIT = 64

_CATEGORY_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RECORD_TYPES = {record_type.value for record_type in RecordType}

_TYPE_TITLES = {ALL_FILTER: "📚 Все типы"}
_TYPE_TITLES.update({record_type.value: record_type.value.title() for record_type in RecordType})


def parse_archive_callback(data: str | None) -> tuple[str, str, str, int] | None:
    """Разбирает archive:l:{status}:{category}:{type}:{page}.

    Возвращает (status_code, category_code, record_type, page) или None
    для устаревших/некорректных callback_data.
    """
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 6 or parts[0] != "archive" or parts[1] != "l":
        return None
    _, _, status, category, record_type, page_raw = parts
    if status not in STATUS_CODES:
        return None
    if category != ALL_FILTER and not _CATEGORY_CODE_RE.match(category):
        return None
    if record_type != ALL_FILTER and record_type not in _RECORD_TYPES:
        return None
    try:
        page = int(page_raw)
    except ValueError:
        return None
    if page < 0:
        return None
    return status, category, record_type, page


def build_archive_keyboard(
    records_with_categories: list[tuple[Record, str]],
    *,
    total: int,
    page: int,
    status: str,
    category: str,
    record_type: str,
    can_restore: bool,
    can_purge: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура экрана архива: карточки, восстановление/удаление, фильтры, страницы."""
    rows: list[list[InlineKeyboardButton]] = []
    for record, category_name in records_with_categories:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {record.title[:40]} · {category_name[:16]}",
                    callback_data=f"record:view:{record.id}",
                )
            ]
        )
        actions: list[InlineKeyboardButton] = []
        if can_restore:
            actions.append(
                InlineKeyboardButton(
                    text="♻️ Восстановить",
                    callback_data=f"archive:restore:{record.id}",
                )
            )
        if can_purge:
            actions.append(
                InlineKeyboardButton(
                    text="☠️ Удалить окончательно",
                    callback_data=f"archive:purge:{record.id}",
                )
            )
        if actions:
            rows.append(actions)

    tabs = [
        InlineKeyboardButton(
            text=title,
            callback_data=f"archive:l:{code}:{category}:{record_type}:0",
        )
        for code, title in STATUS_TITLES.items()
    ]
    rows.append(tabs)
    rows.append(
        [
            InlineKeyboardButton(
                text="📂 Категория ▾",
                callback_data=f"archive:fc:{status}:{record_type}",
            ),
            InlineKeyboardButton(
                text="📚 Тип ▾",
                callback_data=f"archive:ft:{status}:{category}",
            ),
        ]
    )

    prefix = f"archive:l:{status}:{category}:{record_type}"
    rows.extend(pagination_keyboard(total, page, PAGE_SIZE, prefix).inline_keyboard)
    rows.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def after_action_keyboard() -> InlineKeyboardMarkup:
    """Возврат на экран архива и в главное меню после восстановления/удаления."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗃 К архиву", callback_data="archive:open"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main"),
            ]
        ]
    )


async def _guard_user(
    session,
    telegram_id: int,
    permission: str,
    callback: CallbackQuery,
) -> User | None:
    """Проверяет активность и системное право; при отказе сам отвечает на callback."""
    user = await get_user_by_telegram_id(session, telegram_id)
    if not await is_user_active(user):
        await callback.answer("Нет доступа", show_alert=True)
        return None
    if not await PermissionService.can(user, permission, None, None, session):
        await callback.answer("Недостаточно прав", show_alert=True)
        return None
    return user


async def _archive_access(callback: CallbackQuery, session) -> bool:
    """Право ARCHIVE даёт доступ к экрану архива (Default Deny)."""
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    return await is_user_active(user) and await PermissionService.can(
        user, "ARCHIVE", None, None, session
    )


async def _render_archive(
    callback: CallbackQuery,
    status: str,
    category: str,
    record_type: str,
    page: int,
) -> bool:
    """Рисует страницу скрытых записей. False — доступ отказан или данные устарели."""
    status_value = STATUS_CODES.get(status)
    if status_value is None:
        return False
    if category != ALL_FILTER and not _CATEGORY_CODE_RE.match(category):
        return False
    if record_type != ALL_FILTER and record_type not in _RECORD_TYPES:
        return False
    page = max(page, 0)

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user) or not await PermissionService.can(
            user, "ARCHIVE", None, None, session
        ):
            return False

        conditions = [Record.status == status_value]
        if category != ALL_FILTER:
            conditions.append(Category.code == category)
        if record_type != ALL_FILTER:
            conditions.append(Record.record_type == RecordType(record_type))

        total = (
            await session.scalar(
                select(func.count())
                .select_from(Record)
                .join(Category, Category.id == Record.category_id)
                .where(*conditions)
            )
            or 0
        )
        rows_data = (
            (
                await session.execute(
                    select(Record, Category.name)
                    .join(Category, Category.id == Record.category_id)
                    .where(*conditions)
                    .order_by(Record.updated_at.desc(), Record.id)
                    .offset(page * PAGE_SIZE)
                    .limit(PAGE_SIZE)
                )
            )
            .all()
        )
        can_restore = await PermissionService.can(user, "RESTORE", None, None, session)
        can_purge = await PermissionService.can(user, "DELETE", None, None, session)

    status_title = STATUS_TITLES[status]
    filter_category = "все разделы" if category == ALL_FILTER else category
    filter_type = "все типы" if record_type == ALL_FILTER else _TYPE_TITLES[record_type]
    lines = [
        breadcrumbs(status_title),
        "",
        f"Всего: {total}. Фильтры: {filter_category} · {filter_type}.",
    ]
    if not rows_data:
        lines += ["", "Здесь пусто."]

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=build_archive_keyboard(
            rows_data,
            total=total,
            page=page,
            status=status,
            category=category,
            record_type=record_type,
            can_restore=can_restore,
            can_purge=can_purge,
        ),
    )
    return True


@router.callback_query(F.data == "menu:archive")
async def archive_from_menu(callback: CallbackQuery) -> None:
    """Точка входа главного меню «🗃 Архив» (подменяет упрощённый экран Phase 04)."""
    if not await _render_archive(callback, "a", ALL_FILTER, ALL_FILTER, 0):
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "archive:open")
async def archive_open(callback: CallbackQuery) -> None:
    """Первая страница архива без фильтров."""
    if not await _render_archive(callback, "a", ALL_FILTER, ALL_FILTER, 0):
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("archive:l:"))
async def archive_list(callback: CallbackQuery) -> None:
    parsed = parse_archive_callback(callback.data)
    if parsed is None or not await _render_archive(callback, *parsed):
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("archive:fc:"))
async def archive_filter_category(callback: CallbackQuery) -> None:
    """Экран фильтра по категории: текущие статус и тип сохраняются."""
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[2] not in STATUS_CODES:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    if parts[3] != ALL_FILTER and parts[3] not in _RECORD_TYPES:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    status, record_type = parts[2], parts[3]

    async with async_session() as session:
        if not await _archive_access(callback, session):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        categories = (
            (
                await session.execute(
                    select(Category)
                    .where(Category.status == "ACTIVE")
                    .order_by(Category.sort_order, Category.name)
                )
            )
            .scalars()
            .all()
        )

    rows = [
        [
            InlineKeyboardButton(
                text="📚 Все разделы",
                callback_data=f"archive:l:{status}:{ALL_FILTER}:{record_type}:0",
            )
        ]
    ]
    for cat in categories:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{category_emoji(cat.code)} {cat.name[:28]}",
                    callback_data=f"archive:l:{status}:{cat.code}:{record_type}:0",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"archive:l:{status}:{ALL_FILTER}:{record_type}:0"
            )
        ]
    )
    await callback.message.edit_text(
        "📂 Выберите раздел для фильтра:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("archive:ft:"))
async def archive_filter_type(callback: CallbackQuery) -> None:
    """Экран фильтра по типу информации внутри текущего раздела."""
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[2] not in STATUS_CODES:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    status, category = parts[2], parts[3]
    if category != ALL_FILTER and not _CATEGORY_CODE_RE.match(category):
        await callback.answer(STALE_TEXT, show_alert=True)
        return

    rows = [
        [
            InlineKeyboardButton(
                text=_TYPE_TITLES[ALL_FILTER],
                callback_data=f"archive:l:{status}:{category}:{ALL_FILTER}:0",
            )
        ]
    ]
    for record_type in RecordType:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_TYPE_TITLES[record_type.value],
                    callback_data=f"archive:l:{status}:{category}:{record_type.value}:0",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"archive:l:{status}:{category}:{ALL_FILTER}:0"
            )
        ]
    )
    await callback.message.edit_text(
        "📚 Выберите тип информации:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("archive:restore:"))
async def archive_restore(callback: CallbackQuery) -> None:
    """Восстановление записи из архива или корзины."""
    rid = (callback.data or "").split(":")[-1]
    try:
        record_uuid = UUID(rid)
    except ValueError:
        await callback.answer(STALE_TEXT, show_alert=True)
        return

    async with async_session() as session:
        user = await _guard_user(session, callback.from_user.id, "RESTORE", callback)
        if user is None:
            return
        try:
            record = await restore_record(session, record_uuid, user.id, user=user)
        except RecordError as e:
            await callback.answer(str(e), show_alert=True)
            return
        if record is None:
            await callback.answer("Запись не найдена или не скрыта", show_alert=True)
            return
        await log_audit(session, user.id, "RESTORE_RECORD", "Record", rid)

    await callback.answer("Запись восстановлена")
    await callback.message.edit_text(
        f"♻️ Запись «{html.escape(record.title)}» восстановлена в активный список.",
        reply_markup=after_action_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("archive:purge:"))
async def archive_purge_ask(callback: CallbackQuery) -> None:
    """Запрос подтверждения окончательного удаления (ТЗ: удаление только с подтверждением)."""
    rid = (callback.data or "").split(":")[-1]
    try:
        record_uuid = UUID(rid)
    except ValueError:
        await callback.answer(STALE_TEXT, show_alert=True)
        return

    async with async_session() as session:
        if await _guard_user(session, callback.from_user.id, "DELETE", callback) is None:
            return
        record = await session.get(Record, record_uuid)
        if record is None:
            await callback.answer("Запись не найдена", show_alert=True)
            return
        if record.status not in PERMANENTLY_DELETABLE_STATUSES:
            await callback.answer(
                "Активные записи удалять нельзя — сначала архивируйте.", show_alert=True
            )
            return

    await callback.message.edit_text(
        f"☠️ <b>Удалить запись «{html.escape(record.title)}» окончательно?</b>\n\n"
        "Поля, теги и версии будут стёрты безвозвратно. Восстановление "
        "останется возможным только из резервных копий.",
        reply_markup=confirm_keyboard(f"archive:purge_yes:{rid}", f"record:view:{rid}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("archive:purge_yes:"))
async def archive_purge_confirm(callback: CallbackQuery) -> None:
    """Окончательное удаление после подтверждения."""
    rid = (callback.data or "").split(":")[-1]
    try:
        record_uuid = UUID(rid)
    except ValueError:
        await callback.answer(STALE_TEXT, show_alert=True)
        return

    async with async_session() as session:
        user = await _guard_user(session, callback.from_user.id, "DELETE", callback)
        if user is None:
            return
        try:
            await permanent_delete(session, record_uuid, user.id, user=user)
        except RecordError as e:
            await callback.answer(str(e), show_alert=True)
            return
        await log_audit(session, user.id, "PERMANENT_DELETE_RECORD", "Record", rid)

    await callback.answer("Удалено окончательно")
    await callback.message.edit_text(
        "☠️ Запись удалена окончательно.",
        reply_markup=after_action_keyboard(),
    )


__all__ = [
    "ALL_FILTER",
    "CALLBACK_LIMIT",
    "STATUS_CODES",
    "STATUS_TITLES",
    "after_action_keyboard",
    "archive_filter_category",
    "archive_filter_type",
    "archive_from_menu",
    "archive_list",
    "archive_open",
    "archive_purge_ask",
    "archive_purge_confirm",
    "archive_restore",
    "build_archive_keyboard",
    "parse_archive_callback",
    "router",
]
