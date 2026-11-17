from datetime import UTC, datetime
from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.layout import smart_layout
from app.core.config import settings
from app.database.models import (
    RegistrationRequest,
    RegistrationRequestStatus,
    Role,
    RoleCode,
    User,
    UserRole,
    UserStatus,
)
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active

router = Router()

ROLE_OPTIONS = [
    ("holodka", "Холодка"),
    ("second_stage", "2 этап"),
    ("closers", "Клоузер"),
]


class Registration(StatesGroup):
    name = State()
    role = State()
    confirm = State()
    edit_name = State()
    edit_role = State()
    edit_confirm = State()


def _request_markup(request_id: UUID) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"req:a:{request_id}"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"req:e:{request_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req:r:{request_id}"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))


async def _notify_admins(bot, text: str, request_id: UUID) -> None:
    ids = [settings.superadmin_telegram_id]
    async with async_session() as session:
        result = await session.execute(
            __import__("sqlalchemy").select(User.telegram_id).where(
                User.role.in_([RoleCode.SUPERADMIN, RoleCode.COACH])
            )
        )
        ids.extend(result.scalars().all())
    for tid in set(ids):
        try:
            await bot.send_message(chat_id=tid, text=text, reply_markup=_request_markup(request_id))
        except Exception:  # noqa: BLE001, S110
            pass


@router.callback_query(F.data == "reg:start")
async def reg_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(Registration.name)
    await callback.message.answer("Введите ваше имя сотрудника:")


@router.message(Registration.name)
async def reg_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Введите имя:")
        return
    await state.update_data(name=name)
    await state.set_state(Registration.role)
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"reg:role:{code}")]
        for code, title in ROLE_OPTIONS
    ]
    await message.answer("Выберите должность:", reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(rows)))


@router.callback_query(Registration.role, F.data.startswith("reg:role:"))
async def reg_role(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    code = (callback.data or "").removeprefix("reg:role:")
    if code not in {c for c, _ in ROLE_OPTIONS}:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    title = dict(ROLE_OPTIONS)[code]
    await state.update_data(role_code=code, role_title=title)
    await state.set_state(Registration.confirm)
    data = await state.get_data()
    text = (
        "📋 <b>Подтвердите данные</b>\n\n"
        f"Имя: <b>{data['name']}</b>\n"
        f"Должность: <b>{title}</b>\n\n"
        "Отправить запрос на регистрацию?"
    )
    rows = [
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="reg:confirm"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data="reg:start"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="reg:cancel"),
        ]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(rows)), parse_mode="HTML")


@router.callback_query(F.data == "reg:cancel")
async def reg_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await state.clear()
    await callback.message.edit_text("Запрос на регистрацию отменён.")


async def _create_request(message_or_callback, state: FSMContext) -> RegistrationRequest:
    data = await state.get_data()
    user = message_or_callback.from_user
    async with async_session() as session:
        req = RegistrationRequest(
            telegram_id=user.id,
            username=user.username,
            display_name=data["name"],
            role_code=data["role_code"],
        )
        session.add(req)
        await session.commit()
        await session.refresh(req)
    title = data["role_title"]
    await _notify_admins(
        message_or_callback.bot,
        f"📥 <b>Новая заявка на регистрацию</b>\n\n"
        f"Имя: <b>{data['name']}</b>\n"
        f"Telegram ID: <code>{user.id}</code>\n"
        f"Username: @{user.username or '—'}\n"
        f"Должность: <b>{title}</b>",
        req.id,
    )
    await state.clear()
    return req


@router.callback_query(F.data == "reg:confirm")
async def reg_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if not data.get("name") or not data.get("role_code"):
        await callback.answer("Состояние устарело. Начните регистрацию заново.", show_alert=True)
        return
    await _create_request(callback, state)
    await callback.message.edit_text("✅ Запрос на регистрацию отправлен администратору. Ожидайте решения.")


# --- Админ: список заявок ---
@router.callback_query(F.data == "reg:list")
async def reg_list(callback: CallbackQuery) -> None:
    await callback.answer()
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value not in ("SUPERADMIN", "COACH"):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from sqlalchemy import select

        reqs = (
            await session.execute(
                select(RegistrationRequest)
                .where(RegistrationRequest.status == RegistrationRequestStatus.PENDING)
                .order_by(RegistrationRequest.created_at)
            )
        ).scalars().all()
    if not reqs:
        await callback.message.edit_text("📥 Новых заявок на регистрацию нет.")
        return
    lines = ["📥 <b>Заявки на регистрацию</b>\n"]
    rows = []
    for r in reqs:
        title = dict(ROLE_OPTIONS).get(r.role_code, r.role_code)
        lines.append(f"• {r.display_name} — {title} — <code>{r.telegram_id}</code>")
        rows.append([InlineKeyboardButton(text=f"{r.display_name} — {title}", callback_data=f"req:v:{r.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(rows)), parse_mode="HTML")


@router.callback_query(F.data.startswith("req:v:"))
async def req_view(callback: CallbackQuery) -> None:
    await callback.answer()
    rid = (callback.data or "").removeprefix("req:v:")
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value not in ("SUPERADMIN", "COACH"):
            await callback.answer("Нет доступа", show_alert=True)
            return
        req = await session.get(RegistrationRequest, UUID(rid))
        if req is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
    title = dict(ROLE_OPTIONS).get(req.role_code, req.role_code)
    text = (
        f"📋 <b>Заявка</b>\n\n"
        f"Имя: <b>{req.display_name}</b>\n"
        f"Telegram ID: <code>{req.telegram_id}</code>\n"
        f"Username: @{req.username or '—'}\n"
        f"Должность: <b>{title}</b>\n"
        f"Статус: {req.status.value}"
    )
    await callback.message.edit_text(text, reply_markup=_request_markup(req.id), parse_mode="HTML")


async def _approve_request(callback: CallbackQuery, req: RegistrationRequest, name: str | None = None, role_code: str | None = None) -> None:
    target_role_code = role_code or req.role_code
    async with async_session() as session:
        existing = await get_user_by_telegram_id(session, req.telegram_id)
        emp_role = (await session.execute(__import__("sqlalchemy").select(Role).where(Role.code == RoleCode.EMPLOYEE.value))).scalar_one_or_none()
        if existing is None:
            user = User(
                telegram_id=req.telegram_id,
                username=req.username,
                display_name=name or req.display_name,
                role=RoleCode.EMPLOYEE,
                role_id=emp_role.id,
                status=UserStatus.ACTIVE,
            )
            session.add(user)
            await session.flush()
        else:
            user = existing
            user.status = UserStatus.ACTIVE
            if name:
                user.display_name = name
        # data role
        data_role = (await session.execute(__import__("sqlalchemy").select(Role).where(Role.code == target_role_code))).scalar_one_or_none()
        if data_role is not None:
            has = await session.execute(
                __import__("sqlalchemy").select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == data_role.id)
            )
            if has.scalar_one_or_none() is None:
                session.add(UserRole(user_id=user.id, role_id=data_role.id))
        req.status = RegistrationRequestStatus.APPROVED
        req.decided_at = datetime.now(UTC)
        req.decided_by = (await get_user_by_telegram_id(session, callback.from_user.id)).id
        await session.commit()
    try:
        await callback.bot.send_message(chat_id=req.telegram_id, text="✅ Ваша заявка на регистрацию одобрена. Отправьте /start для входа.")
    except Exception:  # noqa: BLE001, S110
        pass


@router.callback_query(F.data.startswith("req:a:"))
async def req_approve(callback: CallbackQuery) -> None:
    await callback.answer()
    rid = (callback.data or "").removeprefix("req:a:")
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value not in ("SUPERADMIN", "COACH"):
            await callback.answer("Нет доступа", show_alert=True)
            return
        req = await session.get(RegistrationRequest, UUID(rid))
        if req is None or req.status != RegistrationRequestStatus.PENDING:
            await callback.answer("Заявка недоступна", show_alert=True)
            return
        await _approve_request(callback, req)
    await callback.message.edit_text("✅ Заявка одобрена, сотрудник зарегистрирован.")


@router.callback_query(F.data.startswith("req:r:"))
async def req_reject(callback: CallbackQuery) -> None:
    await callback.answer()
    rid = (callback.data or "").removeprefix("req:r:")
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value not in ("SUPERADMIN", "COACH"):
            await callback.answer("Нет доступа", show_alert=True)
            return
        req = await session.get(RegistrationRequest, UUID(rid))
        if req is None or req.status != RegistrationRequestStatus.PENDING:
            await callback.answer("Заявка недоступна", show_alert=True)
            return
        req.status = RegistrationRequestStatus.REJECTED
        req.decided_at = datetime.now(UTC)
        req.decided_by = me.id
        await session.commit()
    try:
        await callback.bot.send_message(chat_id=req.telegram_id, text="❌ Ваша заявка на регистрацию отклонена.")
    except Exception:  # noqa: BLE001, S110
        pass
    await callback.message.edit_text("❌ Заявка отклонена.")


@router.callback_query(F.data.startswith("req:e:"))
async def req_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    rid = (callback.data or "").removeprefix("req:e:")
    await state.update_data(edit_request_id=rid)
    await state.set_state(Registration.edit_name)
    await callback.message.answer("Введите имя сотрудника (для редактирования заявки):")


@router.message(Registration.edit_name)
async def req_edit_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым.")
        return
    await state.update_data(edit_name=name)
    await state.set_state(Registration.edit_role)
    rows = [[InlineKeyboardButton(text=title, callback_data=f"editrole:{code}")] for code, title in ROLE_OPTIONS]
    await message.answer("Выберите должность:", reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(rows)))


@router.callback_query(Registration.edit_role, F.data.startswith("editrole:"))
async def req_edit_role(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    code = (callback.data or "").removeprefix("editrole:")
    if code not in {c for c, _ in ROLE_OPTIONS}:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    await state.update_data(edit_role_code=code, edit_role_title=dict(ROLE_OPTIONS)[code])
    await state.set_state(Registration.edit_confirm)
    data = await state.get_data()
    await callback.message.edit_text(
        f"Имя: <b>{data['edit_name']}</b>\nДолжность: <b>{data['edit_role_title']}</b>\n\nЗарегистрировать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Зарегистрировать", callback_data="editreg:confirm")]]),
        parse_mode="HTML",
    )


@router.callback_query(Registration.edit_confirm, F.data == "editreg:confirm")
async def req_edit_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    rid = data.get("edit_request_id")
    async with async_session() as session:
        req = await session.get(RegistrationRequest, UUID(rid))
        if req is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            await state.clear()
            return
        await _approve_request(callback, req, name=data["edit_name"], role_code=data["edit_role_code"])
    await state.clear()
    await callback.message.edit_text("✅ Сотрудник зарегистрирован по указанным данным.")


@router.message()
async def unknown_user_any_message(message: Message) -> None:
    """Незарегистрированный пользователь: предложить заявку на любом сообщении."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
    if user is not None and await is_user_active(user):
        return
    if user is not None:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Зарегистрировать", callback_data="reg:start")]]
    )
    await message.answer(
        "Вы не зарегистрированы. Отправьте запрос на регистрацию:",
        reply_markup=kb,
    )
