from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from app.bot.keyboards.layout import smart_layout
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database.models import Role, RoleCode, User, UserStatus
from app.database.session import async_session
from app.services.auth.admin import (
    assign_user_to_group,
    get_user_by_id,
    get_user_groups,
    list_groups,
    list_users,
    remove_user_from_group,
    set_user_role,
    set_user_status,
)
from app.services.auth.permissions import get_user_by_telegram_id

router = Router()


@router.callback_query(F.data == "admin:roles")
async def admin_roles(callback: CallbackQuery) -> None:
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Только SUPERADMIN", show_alert=True)
            return
        groups = await list_groups(session)

    buttons = []
    for g in groups:
        buttons.append([InlineKeyboardButton(text=f"👥 {g.name}", callback_data=f"admin:groupview:{g.id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    await callback.message.edit_text("🛡 Роли и доступы — группы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(buttons)))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:groupview:"))
async def admin_group_view(callback: CallbackQuery) -> None:
    gid = (callback.data or "").split(":")[-1]
    from uuid import UUID

    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from sqlalchemy import select

        from app.database.models import Group, User

        group = await session.get(Group, UUID(gid))
        if not group:
            await callback.answer("Не найдена")
            return
        # users in group
        from app.database.models import GroupMember

        result = await session.execute(
            select(User).join(GroupMember, GroupMember.user_id == User.id).where(GroupMember.group_id == UUID(gid))
        )
        users = result.scalars().all()

    lines = [f"👥 <b>{group.name}</b>\n"]
    if not users:
        lines.append("Пользователей нет.")
    else:
        for u in users:
            lines.append(f"• {u.display_name or u.telegram_id} — {u.role.value}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:roles")]])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


class AddUser(StatesGroup):
    waiting_telegram_id = State()
    waiting_name = State()
    waiting_role = State()


def _admin_only(user) -> bool:
    return user and user.role == RoleCode.SUPERADMIN


@router.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery) -> None:
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Только SUPERADMIN", show_alert=True)
            return
        users = await list_users(session)

    lines = ["👥 Пользователи:\n"]
    buttons = []
    for u in users:
        status = "🟢" if u.status == UserStatus.ACTIVE else "🔴"
        lines.append(f"{status} {u.display_name or u.username or u.telegram_id} — {u.role.value}")
        buttons.append([InlineKeyboardButton(text=f"{u.display_name or u.telegram_id}", callback_data=f"admin:user:{u.id}")])
    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="admin:user:add")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])

    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(buttons)))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:") & ~F.data.endswith(":add"))
async def admin_user_detail(callback: CallbackQuery) -> None:
    uid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Нет доступа", show_alert=True)
            return
        user = await get_user_by_id(session, UUID(uid))
        if not user:
            await callback.answer("Не найден")
            return
        groups = await get_user_groups(session, user.id)

    group_names = ", ".join(g.name for g in groups) or "нет"
    text = (
        f"👤 <b>{user.display_name or user.telegram_id}</b>\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Роль: {user.role.value}\n"
        f"Статус: {user.status.value}\n"
        f"Группы: {group_names}"
    )
    buttons = []
    if user.role != RoleCode.SUPERADMIN:
        buttons.extend(
            [
                [InlineKeyboardButton(text="Сменить роль", callback_data=f"admin:role:{user.id}")],
                [
                    InlineKeyboardButton(
                        text="Заблокировать"
                        if user.status == UserStatus.ACTIVE
                        else "Разблокировать",
                        callback_data=f"admin:toggle:{user.id}",
                    )
                ],
            ]
        )
    buttons.extend(
        [
            [InlineKeyboardButton(text="Группы", callback_data=f"admin:groups:{user.id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin:users")],
        ]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=smart_layout(buttons))
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:toggle:"))
async def toggle_status(callback: CallbackQuery) -> None:
    uid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Нет доступа", show_alert=True)
            return
        user = await get_user_by_id(session, UUID(uid))
        if not user:
            await callback.answer()
            return
        if user.role == RoleCode.SUPERADMIN:
            await callback.answer("Настроенного SUPERADMIN нельзя блокировать", show_alert=True)
            return
        new_status = UserStatus.BLOCKED if user.status == UserStatus.ACTIVE else UserStatus.ACTIVE
        await set_user_status(session, user.id, new_status)
    await callback.answer(f"Статус изменён на {new_status.value}")
    await admin_user_detail(callback)


@router.callback_query(F.data.startswith("admin:role:"))
async def change_role_menu(callback: CallbackQuery) -> None:
    uid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Нет доступа", show_alert=True)
            return
        target = await get_user_by_id(session, UUID(uid))
        if target is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        if target.role == RoleCode.SUPERADMIN:
            await callback.answer("Роль настроенного SUPERADMIN нельзя изменить", show_alert=True)
            return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="EMPLOYEE", callback_data=f"admin:setrole:{uid}:EMPLOYEE")],
        [InlineKeyboardButton(text="BOSS", callback_data=f"admin:setrole:{uid}:BOSS")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:user:{uid}")],
    ])
    await callback.message.edit_text("Выберите роль:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:setrole:"))
async def set_role(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    uid, role_name = parts[2], parts[3]
    if role_name not in {RoleCode.EMPLOYEE.value, RoleCode.BOSS.value}:
        await callback.answer("SUPERADMIN назначается только конфигурацией сервера", show_alert=True)
        return
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Нет доступа", show_alert=True)
            return
        target = await get_user_by_id(session, UUID(uid))
        if not target:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        if target.role == RoleCode.SUPERADMIN:
            await callback.answer("Роль настроенного SUPERADMIN нельзя изменить", show_alert=True)
            return
        await set_user_role(session, UUID(uid), RoleCode(role_name))
    await callback.answer(f"Роль установлена: {role_name}")
    callback.data = f"admin:user:{uid}"
    await admin_user_detail(callback)


@router.callback_query(F.data.startswith("admin:groups:"))
async def manage_groups(callback: CallbackQuery) -> None:
    uid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Нет доступа", show_alert=True)
            return
        all_groups = await list_groups(session)
        user_groups = await get_user_groups(session, UUID(uid))
        user_group_ids = {g.id for g in user_groups}

    buttons = []
    for g in all_groups:
        mark = "✅" if g.id in user_group_ids else "⬜"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {g.name}",
            callback_data=f"admin:togglegroup:{uid}:{g.id}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:user:{uid}")])
    await callback.message.edit_text("Группы пользователя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(buttons)))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:togglegroup:"))
async def toggle_group(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    uid, gid = parts[2], parts[3]
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Нет доступа", show_alert=True)
            return
        user_groups = await get_user_groups(session, UUID(uid))
        has = any(str(g.id) == gid for g in user_groups)
        if has:
            await remove_user_from_group(session, UUID(uid), UUID(gid))
        else:
            await assign_user_to_group(session, UUID(uid), UUID(gid), me.id)
    await callback.answer("Обновлено")
    callback.data = f"admin:groups:{uid}"
    await manage_groups(callback)


@router.callback_query(F.data == "admin:user:add")
async def start_add_user(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer("Нет доступа", show_alert=True)
            return
    await state.set_state(AddUser.waiting_telegram_id)
    await callback.message.answer("Введите Telegram ID нового пользователя:")
    await callback.answer()


@router.message(AddUser.waiting_telegram_id)
async def add_user_id(message: Message, state: FSMContext) -> None:
    try:
        tid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно число. Попробуйте ещё раз:")
        return
    if tid <= 0:
        await message.answer("Telegram ID должен быть положительным числом. Попробуйте ещё раз:")
        return
    await state.update_data(telegram_id=tid)
    await state.set_state(AddUser.waiting_name)
    await message.answer("Введите отображаемое имя:")


@router.message(AddUser.waiting_name)
async def add_user_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    await state.update_data(display_name=name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="EMPLOYEE", callback_data="admin:newrole:EMPLOYEE")],
        [InlineKeyboardButton(text="BOSS", callback_data="admin:newrole:BOSS")],
    ])
    await state.set_state(AddUser.waiting_role)
    await message.answer("Выберите роль:", reply_markup=kb)


@router.callback_query(F.data.startswith("admin:newrole:"))
async def finish_add_user(callback: CallbackQuery, state: FSMContext) -> None:
    role_name = (callback.data or "").split(":")[-1]
    if role_name not in {RoleCode.EMPLOYEE.value, RoleCode.BOSS.value}:
        await callback.answer("Нельзя назначить эту роль через добавление пользователя", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = data.get("telegram_id")
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        await state.clear()
        await callback.answer("Состояние добавления пользователя устарело. Начните заново.", show_alert=True)
        return
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if not _admin_only(me):
            await callback.answer()
            return
        existing = await session.execute(select(User.id).where(User.telegram_id == telegram_id))
        if existing.scalar_one_or_none():
            await state.clear()
            await callback.answer("Пользователь с таким Telegram ID уже зарегистрирован", show_alert=True)
            return
        role_result = await session.execute(select(Role).where(Role.code == role_name))
        role = role_result.scalar_one_or_none()
        if role is None:
            await callback.answer("Роль не найдена", show_alert=True)
            return
        user = User(
            telegram_id=telegram_id,
            display_name=data.get("display_name"),
            role=RoleCode(role_name),
            role_id=role.id,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await state.clear()
            await callback.answer("Пользователь с таким Telegram ID уже зарегистрирован", show_alert=True)
            return
    await state.clear()
    await callback.message.answer(f"✅ Пользователь {data.get('display_name')} добавлен.")
    await callback.answer()
