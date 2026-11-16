import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.menu import (
    STALE_TEXT,
    TYPE_TITLES,
    breadcrumbs,
    category_title,
    render_category,
    render_main,
)
from app.bot.keyboards.layout import smart_layout
from app.bot.keyboards.pagination import pagination_keyboard, total_pages
from app.database.models import Record, RecordType, User
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.categories import get_category
from app.services.permissions import PermissionService
from app.services.search.service import search_records

router = Router()

SECTION_PREFIX = "search:section:"
FILTER_PREFIX = "search:filter:"
PAGE_PREFIX = "search:page:"
PAGE_SIZE = 5
# Сервис применяет права до ограничения выдачи, поэтому запас нужен только для страниц.
MAX_RESULTS = 200
MAX_QUERY_LENGTH = 256

# ALL + все типы записей — допустимые значения фильтра (ТЗ §7.5 «Фильтрация»)
FILTERS: tuple[str, ...] = ("ALL", *(record_type.value for record_type in RecordType))


class Search(StatesGroup):
    waiting_query = State()
    waiting_section_query = State()


def parse_section_callback(data: str | None) -> str | None:
    """Возвращает код раздела из search:section:{category} или None."""
    if not data or not data.startswith(SECTION_PREFIX):
        return None
    code = data.removeprefix(SECTION_PREFIX)
    return code or None


def parse_filter_callback(data: str | None) -> str | None:
    """Нормализует search:filter:{type} к значению RecordType или ALL."""
    if not data or not data.startswith(FILTER_PREFIX):
        return None
    value = data.removeprefix(FILTER_PREFIX).upper()
    return value if value in FILTERS else None


def parse_page_callback(data: str | None) -> int | None:
    """Возвращает номер страницы из search:page:{n} или None для устаревших кнопок."""
    if not data or not data.startswith(PAGE_PREFIX):
        return None
    raw = data.removeprefix(PAGE_PREFIX)
    return int(raw) if raw.isdigit() else None


def normalize_filter(value: str | None) -> str:
    """Приводит сохранённый фильтр к допустимому значению (защита от старых состояний)."""
    normalized = str(value or "ALL").strip().upper()
    return normalized if normalized in FILTERS else "ALL"


def normalize_page(value: object) -> int:
    """Безопасно разбирает номер страницы из FSM; мусор превращается в первую страницу."""
    try:
        page = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(page, 0)


def filter_rows(active: str) -> list[list[InlineKeyboardButton]]:
    """Ряды кнопок-фильтров по типу данных, текущий фильтр подсвечен."""
    row: list[InlineKeyboardButton] = []
    for value in FILTERS:
        label = "📂 Все" if value == "ALL" else TYPE_TITLES[RecordType(value)].split(" ", 1)[0]
        if value == active:
            label = f"• {label}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"{FILTER_PREFIX}{value.lower()}"))
    return [row[i : i + 4] for i in range(0, len(row), 4)]


def _cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="menu:cancel")],
        ]
    )


def _results_markup(
    records: list[Record], total: int, page: int, filter_type: str
) -> InlineKeyboardMarkup:
    """Кнопки результатов, пагинация, фильтры и навигация."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=f"📄 {record.title}", callback_data=f"record:view:{record.id}")]
        for record in records
    ]
    rows.extend(
        pagination_keyboard(total, page, PAGE_SIZE, PAGE_PREFIX.rstrip(":")).inline_keyboard
    )
    rows.extend(filter_rows(filter_type))
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="search:back"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))


async def _visible_records(
    session: AsyncSession, user: User, query: str, category_code: str | None, filter_type: str
) -> list[Record]:
    """Результаты поиска, уже отфильтрованные по правам в сервисе (Default Deny)."""
    rtype = None if filter_type == "ALL" else RecordType(filter_type)
    return await search_records(
        session,
        query,
        user,
        limit=MAX_RESULTS,
        category_code=category_code,
        record_type=rtype,
    )


async def _render(source: CallbackQuery | Message, state: FSMContext) -> None:
    """Перерисовывает страницу результатов; при пустом состоянии сообщает об устаревании."""
    data = await state.get_data()
    query = str(data.get("query") or "").strip()[:MAX_QUERY_LENGTH]
    if not query:
        await state.clear()
        if isinstance(source, CallbackQuery):
            await source.answer(STALE_TEXT, show_alert=True)
            return
        await source.answer(STALE_TEXT)
        return

    filter_type = normalize_filter(data.get("record_type"))
    category_code = data.get("category")
    page = normalize_page(data.get("page"))

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, source.from_user.id)
        if not await is_user_active(user):
            await state.clear()
            if isinstance(source, CallbackQuery):
                await source.answer("Нет доступа", show_alert=True)
            else:
                await source.answer("Нет доступа")
            return

        section_title = None
        if category_code:
            category = await get_category(session, category_code)
            if category is None or category.status != "ACTIVE":
                await state.clear()
                if isinstance(source, CallbackQuery):
                    await source.answer(STALE_TEXT, show_alert=True)
                else:
                    await source.answer(STALE_TEXT)
                return
            if not await PermissionService.can(
                user, "VIEW", "category", category.id, session
            ):
                # Права могли быть отозваны после открытия поиска
                await state.clear()
                if isinstance(source, CallbackQuery):
                    await source.answer("Нет доступа к разделу", show_alert=True)
                else:
                    await source.answer("Нет доступа к разделу")
                return
            section_title = category_title(category.code, category.name)

        items = await _visible_records(session, user, query, category_code, filter_type)

    total = len(items)
    pages = total_pages(total, PAGE_SIZE)
    page = min(page, pages - 1)
    await state.update_data(page=page, record_type=filter_type)
    window = items[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]

    scope = f"в разделе {section_title}" if section_title else "по всей базе"
    filter_label = "📂 Все типы" if filter_type == "ALL" else TYPE_TITLES[RecordType(filter_type)]
    text = f"{breadcrumbs('🔎 Поиск ' + scope)}\n\n"
    text += f"Запрос: <b>{html.escape(query)}</b>\n"
    text += f"Фильтр: {filter_label}\n\n"
    if total == 0:
        text += "Ничего не найдено. Уточните запрос или снимите фильтр."
    else:
        text += f"Найдено: {total} · страница {page + 1} из {pages}"
        for record in window:
            text += f"\n• {html.escape(record.title)}"

    markup = _results_markup(window, total, page, filter_type)
    if isinstance(source, CallbackQuery):
        try:
            await source.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            # Повторное нажатие на текущую страницу не является ошибкой
            if "message is not modified" not in str(e):
                raise
        await source.answer()
    else:
        await source.answer(text, reply_markup=markup)


@router.callback_query(F.data == "search:start")
async def search_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Search.waiting_query)
    await state.update_data(query=None, category=None, record_type="ALL", page=0)
    await callback.message.answer(
        "🔎 <b>Поиск по всей базе</b>\n\nВведите поисковый запрос:",
        reply_markup=_cancel_markup(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith(SECTION_PREFIX))
async def search_section_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Поиск внутри выбранного раздела: search:section:{category}."""
    code = parse_section_callback(callback.data)
    if code is None:
        await callback.answer(STALE_TEXT, show_alert=True)
        return

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        category = await get_category(session, code)
        if category is None or category.status != "ACTIVE":
            await callback.answer("Раздел не найден", show_alert=True)
            return
        if not await PermissionService.can(user, "VIEW", "category", category.id, session):
            await callback.answer("Нет доступа к разделу", show_alert=True)
            return
        section_title = category_title(category.code, category.name)

    await state.set_state(Search.waiting_section_query)
    await state.update_data(query=None, category=code, record_type="ALL", page=0)
    await callback.message.answer(
        f"🔎 <b>Поиск в разделе {html.escape(section_title)}</b>\n\nВведите поисковый запрос:",
        reply_markup=_cancel_markup(),
    )
    await callback.answer()


@router.message(StateFilter(Search.waiting_query, Search.waiting_section_query))
async def process_query(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if not query:
        await message.answer("Пустой запрос. Попробуйте ещё раз:")
        return
    if query.startswith("/"):
        # Команды не должны «съедаться» FSM-состоянием поиска
        await state.clear()
        await message.answer("🔎 Поиск отменён.")
        return
    await state.update_data(query=query, page=0, record_type="ALL")
    await _render(message, state)


@router.callback_query(F.data.startswith(FILTER_PREFIX))
async def filter_callback(callback: CallbackQuery, state: FSMContext) -> None:
    filter_type = parse_filter_callback(callback.data)
    if filter_type is None or not (await state.get_data()).get("query"):
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    await state.update_data(record_type=filter_type, page=0)
    await _render(callback, state)


@router.callback_query(F.data.startswith(PAGE_PREFIX))
async def page_callback(callback: CallbackQuery, state: FSMContext) -> None:
    page = parse_page_callback(callback.data)
    if page is None or not (await state.get_data()).get("query"):
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    await state.update_data(page=page)
    await _render(callback, state)


@router.callback_query(F.data == "search:back")
async def back_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат: в раздел — если поиск был локальным, иначе в главное меню."""
    data = await state.get_data()
    code = data.get("category")
    await state.clear()
    if code and await render_category(callback, code):
        await callback.answer()
        return
    await render_main(callback)
    await callback.answer()
