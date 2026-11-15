from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.menu import (
    can_manage_categories,
    category_content_keyboard,
    category_emoji,
)
from app.database.models import Category
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id
from app.services.categories import (
    CategoryError,
    CategoryInUseError,
    archive_category,
    count_category_records,
    create_category,
    delete_category,
    get_category,
    get_category_by_id,
    list_categories,
    update_category,
    validate_category_code,
    validate_category_name,
)
from app.services.permissions import PermissionService

router = Router()


class AddCategory(StatesGroup):
    waiting_code = State()
    waiting_name = State()
    waiting_sort_order = State()


class EditCategory(StatesGroup):
    waiting_name = State()
    waiting_sort_order = State()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


async def _callback_manager(session, callback: CallbackQuery):
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    return user if can_manage_categories(user) else None


async def _message_manager(session, message: Message):
    user = await get_user_by_telegram_id(session, message.from_user.id)
    return user if can_manage_categories(user) else None


async def _can_view(session, user, category: Category) -> bool:
    if category.status != "ACTIVE":
        return can_manage_categories(user)
    if user is None:
        return False
    return await PermissionService.can(user, "VIEW", "category", category.id, session)


async def _parent_code(session, category: Category) -> str | None:
    if category.parent_id is None:
        return None
    parent = await get_category_by_id(session, category.parent_id)
    return parent.code if parent else None


def _back_markup(back_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)]]
    )


def _manager_back(parent_code: str | None) -> str:
    if parent_code:
        return f"category:view:{parent_code}"
    return "category:manage"


# ---------------------------------------------------------------------------
# Управление категориями
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "category:manage")
async def category_manage(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await _callback_manager(session, callback)
        if user is None:
            await callback.answer("Только BOSS/SUPERADMIN", show_alert=True)
            return
        roots = await list_categories(session, include_archived=True)

    lines = ["📂 <b>Управление категориями</b>\n"]
    buttons = []
    for root in roots:
        mark = "🗄" if root.status == "ARCHIVED" else category_emoji(root.code)
        lines.append(f"{mark} {escape(root.name)} — <code>{escape(root.code)}</code>")
        row = [
            InlineKeyboardButton(
                text=f"{mark} {root.name}",
                callback_data=f"category:view:{root.code}",
            )
        ]
        if root.status == "ACTIVE":
            row.append(
                InlineKeyboardButton(
                    text="➕",
                    callback_data=f"category:add:{root.id}",
                )
            )
        buttons.append(row)
    if not roots:
        lines.append("Категорий пока нет.")
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("category:view:"))
async def category_view(callback: CallbackQuery) -> None:
    category_code = (callback.data or "").split(":", 2)[-1]

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        category = await get_category(session, category_code)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        if not await _can_view(session, user, category):
            await callback.answer("Нет доступа к категории", show_alert=True)
            return
        manager = can_manage_categories(user)
        children = await list_categories(
            session, category.id, include_archived=manager
        )
        parent_code = await _parent_code(session, category)

    lines = [
        f"{category_emoji(category.code)} <b>{escape(category.name)}</b>",
        f"Код: <code>{escape(category.code)}</code>",
        "",
    ]
    buttons = []
    if children:
        lines.append("Подкатегории:")
        for child in children:
            mark = "🗄" if child.status == "ARCHIVED" else "📂"
            lines.append(f"{mark} {escape(child.name)}")
            row = [
                InlineKeyboardButton(
                    text=f"{mark} {child.name}",
                    callback_data=f"category:view:{child.code}",
                )
            ]
            if manager:
                row.append(
                    InlineKeyboardButton(
                        text="✏️", callback_data=f"category:edit:{child.id}"
                    )
                )
                if child.status == "ACTIVE":
                    row.append(
                        InlineKeyboardButton(
                            text="🗄", callback_data=f"category:archive:{child.id}"
                        )
                    )
                row.append(
                    InlineKeyboardButton(
                        text="🗑", callback_data=f"category:delete:{child.id}"
                    )
                )
            buttons.append(row)
    else:
        lines.append("Подкатегорий пока нет.")

    buttons.append(
        [
            InlineKeyboardButton(
                text="📄 Записи раздела", callback_data=f"category:content:{category.code}"
            )
        ]
    )
    if manager and category.parent_id is None:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="➕ Добавить подкатегорию",
                    callback_data=f"category:add:{category.id}",
                )
            ]
        )
    back_callback = f"category:view:{parent_code}" if parent_code else "menu:main"
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)]
    )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("category:content:"))
async def category_content(callback: CallbackQuery) -> None:
    category_code = (callback.data or "").split(":", 2)[-1]

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        category = await get_category(session, category_code)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        if not await _can_view(session, user, category):
            await callback.answer("Нет доступа к категории", show_alert=True)
            return

    await callback.message.edit_text(
        f"📄 <b>{escape(category.name)}</b>\n\nВыберите тип информации:",
        reply_markup=category_content_keyboard(
            category.code, back_callback=f"category:view:{category.code}"
        ),
        parse_mode="HTML",
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Создание подкатегории (FSM)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "category:add")
async def category_add_root_choice(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await _callback_manager(session, callback)
        if user is None:
            await callback.answer("Только BOSS/SUPERADMIN", show_alert=True)
            return
        roots = await list_categories(session)

    if not roots:
        await callback.answer("Сначала создайте основной раздел", show_alert=True)
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=f"📂 {root.name}", callback_data=f"category:add:{root.id}"
            )
        ]
        for root in roots
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="category:manage")])
    await callback.message.edit_text(
        "В каком разделе создать подкатегорию?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("category:add:"))
async def category_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    parent_id = (callback.data or "").split(":", 2)[-1]

    async with async_session() as session:
        user = await _callback_manager(session, callback)
        if user is None:
            await callback.answer("Только BOSS/SUPERADMIN", show_alert=True)
            return
        parent = await get_category_by_id(session, parent_id)
        if parent is None or parent.status != "ACTIVE":
            await callback.answer("Раздел не найден или недоступен", show_alert=True)
            return
        if parent.parent_id is not None:
            await callback.answer(
                "Подкатегории создаются только внутри основных разделов",
                show_alert=True,
            )
            return

    await state.clear()
    await state.update_data(parent_id=str(parent.id), parent_code=parent.code)
    await state.set_state(AddCategory.waiting_code)
    await callback.message.answer(
        f"Создание подкатегории в разделе «{parent.name}».\n"
        "Введите код (строчные латинские буквы, цифры, «_» или «-»):"
    )
    await callback.answer()


@router.message(AddCategory.waiting_code)
async def category_add_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    try:
        normalized = validate_category_code(code)
    except CategoryError as exc:
        await message.answer(f"❌ {exc}\nВведите другой код:")
        return

    async with async_session() as session:
        user = await _message_manager(session, message)
        if user is None:
            await state.clear()
            await message.answer("Нет прав на управление категориями.")
            return
        data = await state.get_data()
        parent = await get_category_by_id(session, data.get("parent_id"))
        if parent is None or parent.status != "ACTIVE":
            await state.clear()
            await message.answer("Раздел больше недоступен. Начните заново.")
            return
        if await get_category(session, normalized) is not None:
            await message.answer("❌ Категория с таким кодом уже существует. Введите другой код:")
            return

    await state.update_data(code=normalized)
    await state.set_state(AddCategory.waiting_name)
    await message.answer("Введите название подкатегории:")


@router.message(AddCategory.waiting_name)
async def category_add_name(message: Message, state: FSMContext) -> None:
    try:
        name = validate_category_name(message.text or "")
    except CategoryError as exc:
        await message.answer(f"❌ {exc}\nВведите название ещё раз:")
        return

    await state.update_data(name=name)
    await state.set_state(AddCategory.waiting_sort_order)
    await message.answer(
        "Введите порядок сортировки числом (например 10) "
        "или «-» для автоматического:"
    )


@router.message(AddCategory.waiting_sort_order)
async def category_add_finish(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    sort_order = None
    if raw != "-":
        try:
            sort_order = int(raw)
        except ValueError:
            await message.answer("❌ Нужно целое число или «-». Попробуйте ещё раз:")
            return

    data = await state.get_data()
    async with async_session() as session:
        user = await _message_manager(session, message)
        if user is None:
            await state.clear()
            await message.answer("Нет прав на управление категориями.")
            return
        try:
            category = await create_category(
                session,
                code=data.get("code", ""),
                name=data.get("name", ""),
                parent_id=data.get("parent_id"),
                category_type="subcategory",
                sort_order=sort_order,
            )
        except CategoryError as exc:
            await state.clear()
            await message.answer(f"❌ {exc}")
            return

    await state.clear()
    parent_code = data.get("parent_code")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📂 Открыть раздел",
                    callback_data=f"category:view:{parent_code}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📂 Управление категориями", callback_data="category:manage"
                )
            ],
        ]
    )
    await message.answer(
        f"✅ Подкатегория «{category.name}» создана.", reply_markup=kb
    )


# ---------------------------------------------------------------------------
# Редактирование категории (FSM)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("category:edit:"))
async def category_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = (callback.data or "").split(":", 2)[-1]

    async with async_session() as session:
        user = await _callback_manager(session, callback)
        if user is None:
            await callback.answer("Только BOSS/SUPERADMIN", show_alert=True)
            return
        category = await get_category_by_id(session, category_id)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return

    await state.clear()
    await state.update_data(
        category_id=str(category.id),
        category_code=category.code,
        name=None,
        sort_order=None,
    )
    await state.set_state(EditCategory.waiting_name)
    await callback.message.answer(
        f"Редактирование «{category.name}».\n"
        "Введите новое название (или «-», чтобы оставить текущее):"
    )
    await callback.answer()


@router.message(EditCategory.waiting_name)
async def category_edit_name(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw == "-":
        name = None
    else:
        try:
            name = validate_category_name(raw)
        except CategoryError as exc:
            await message.answer(f"❌ {exc}\nВведите название ещё раз:")
            return

    await state.update_data(name=name)
    await state.set_state(EditCategory.waiting_sort_order)
    await message.answer(
        "Введите новый порядок сортировки числом (или «-», чтобы оставить):"
    )


@router.message(EditCategory.waiting_sort_order)
async def category_edit_finish(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    sort_order = None
    if raw != "-":
        try:
            sort_order = int(raw)
        except ValueError:
            await message.answer("❌ Нужно целое число или «-». Попробуйте ещё раз:")
            return

    data = await state.get_data()
    async with async_session() as session:
        user = await _message_manager(session, message)
        if user is None:
            await state.clear()
            await message.answer("Нет прав на управление категориями.")
            return
        try:
            category = await update_category(
                session,
                data.get("category_id", ""),
                name=data.get("name"),
                sort_order=sort_order,
            )
        except CategoryError as exc:
            await state.clear()
            await message.answer(f"❌ {exc}")
            return

    await state.clear()
    kb = _back_markup(f"category:view:{category.code}")
    await message.answer(f"✅ Категория «{category.name}» обновлена.", reply_markup=kb)


# ---------------------------------------------------------------------------
# Архивация и удаление
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("category:archive:"))
async def category_archive(callback: CallbackQuery) -> None:
    category_id = (callback.data or "").split(":", 2)[-1]

    async with async_session() as session:
        user = await _callback_manager(session, callback)
        if user is None:
            await callback.answer("Только BOSS/SUPERADMIN", show_alert=True)
            return
        category = await get_category_by_id(session, category_id)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        if category.parent_id is None:
            await callback.answer("Системный раздел нельзя архивировать", show_alert=True)
            return
        if category.status == "ARCHIVED":
            await callback.answer("Категория уже в архиве", show_alert=True)
            return
        parent_code = await _parent_code(session, category)
        try:
            archived = await archive_category(session, category.id)
        except CategoryError as exc:
            await callback.answer(str(exc), show_alert=True)
            return

    back_callback = _manager_back(parent_code)
    await callback.message.edit_text(
        f"🗄 Подкатегория «{escape(archived.name)}» перемещена в архив.",
        reply_markup=_back_markup(back_callback),
        parse_mode="HTML",
    )
    await callback.answer("Архивировано")


@router.callback_query(F.data.startswith("category:delete:"))
async def category_delete(callback: CallbackQuery) -> None:
    category_id = (callback.data or "").split(":", 2)[-1]

    async with async_session() as session:
        user = await _callback_manager(session, callback)
        if user is None:
            await callback.answer("Только BOSS/SUPERADMIN", show_alert=True)
            return
        category = await get_category_by_id(session, category_id)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        if category.parent_id is None:
            await callback.answer("Системный раздел нельзя удалить", show_alert=True)
            return
        records = await count_category_records(session, category.id)
        parent_code = await _parent_code(session, category)

    lines = [
        "🗑 <b>Удаление подкатегории</b>\n",
        f"Категория: <b>{escape(category.name)}</b> (<code>{escape(category.code)}</code>)",
    ]
    if records:
        lines.append(f"В категории есть записи: {records}. Удаление невозможно.")
    else:
        lines.append("Действие необратимо. Подтвердите удаление.")

    buttons = []
    if records == 0:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"category:delete_confirm:{category.id}",
                )
            ]
        )
    back_callback = f"category:view:{parent_code}" if parent_code else "category:manage"
    buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data=back_callback)])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("category:delete_confirm:"))
async def category_delete_confirm(callback: CallbackQuery) -> None:
    category_id = (callback.data or "").split(":", 2)[-1]

    async with async_session() as session:
        user = await _callback_manager(session, callback)
        if user is None:
            await callback.answer("Только BOSS/SUPERADMIN", show_alert=True)
            return
        category = await get_category_by_id(session, category_id)
        if category is None:
            await callback.answer("Категория уже удалена", show_alert=True)
            return
        parent_code = await _parent_code(session, category)
        try:
            await delete_category(session, category.id)
        except CategoryInUseError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except CategoryError as exc:
            await callback.answer(str(exc), show_alert=True)
            return

    back_callback = f"category:view:{parent_code}" if parent_code else "category:manage"
    await callback.message.edit_text(
        f"✅ Подкатегория «{escape(category.name)}» удалена.",
        reply_markup=_back_markup(back_callback),
        parse_mode="HTML",
    )
    await callback.answer("Удалено")


__all__ = ["router"]
