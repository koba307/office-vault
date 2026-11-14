from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.states.add_record import AddRecord
from app.core.config import settings
from app.database.models import RecordType, RoleCode
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active, user_can_access_category
from app.services.records.service import create_record, list_records

router = Router()


def _type_title(rec_type: str) -> str:
    return {
        "CREDENTIALS": "🔐 Логины и пароли",
        "SIP": "☎️ SIP и телефония",
        "LINKS": "🔗 Ссылки",
        "SETTINGS": "⚙️ Настройки",
        "INSTRUCTIONS": "📚 Инструкции",
        "FILES": "📁 Файлы и документы",
        "OTHER": "🗂 Прочее",
    }.get(rec_type.upper(), rec_type)


@router.callback_query(F.data.startswith("type:"))
async def type_callback(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Некорректный запрос")
        return

    rec_type = parts[1].upper()
    category = parts[2]

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await user_can_access_category(session, user, category):
            await callback.answer("Нет доступа к разделу", show_alert=True)
            return

        try:
            rtype = RecordType(rec_type)
        except ValueError:
            await callback.answer("Неизвестный тип")
            return

        records = await list_records(session, category, rtype)

    lines = [f"{_type_title(rec_type)}\n\nРаздел: {category}\n"]
    if not records:
        lines.append("Записей пока нет.")
    else:
        for r in records:
            lines.append(f"• {r.title}")

    buttons = []
    if user and user.role in (RoleCode.SUPERADMIN, RoleCode.BOSS):
        buttons.append([InlineKeyboardButton(text="➕ Добавить запись", callback_data=f"record:add:{rec_type}:{category}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"menu:category:{category}")])

    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("record:add:"))
async def start_add_record(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    rec_type, category = parts[2], parts[3]

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not user or user.role not in (RoleCode.SUPERADMIN, RoleCode.BOSS):
            await callback.answer("Только BOSS/SUPERADMIN", show_alert=True)
            return

    await state.update_data(record_type=rec_type, category=category)
    await state.set_state(AddRecord.waiting_title)
    await callback.message.answer("Введите название записи:")
    await callback.answer()


@router.message(AddRecord.waiting_title)
async def add_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не может быть пустым. Введите ещё раз:")
        return
    await state.update_data(title=title)
    await state.set_state(AddRecord.waiting_description)
    await message.answer("Введите описание (или «-» чтобы пропустить):")


@router.message(AddRecord.waiting_description)
async def add_description(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()
    if desc == "-":
        desc = None
    await state.update_data(description=desc)
    data = await state.get_data()
    rtype = data.get("record_type", "").upper()

    if rtype == "CREDENTIALS":
        await state.set_state(AddRecord.waiting_fields)
        await message.answer("Введите логин:")
        await state.update_data(field_step="login")
    elif rtype == "SIP":
        await state.set_state(AddRecord.waiting_fields)
        await message.answer("Введите SIP Username:")
        await state.update_data(field_step="sip_username")
    else:
        await _finalize_record(message, state)


@router.message(AddRecord.waiting_fields)
async def add_fields(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    step = data.get("field_step")
    value = (message.text or "").strip()
    fields = data.get("fields", {})
    secrets = set(data.get("secrets", []))

    if step == "login":
        fields["login"] = value
        await state.update_data(fields=fields, field_step="password", secrets=list(secrets))
        await message.answer("Введите пароль:")
        return
    if step == "password":
        fields["password"] = value
        secrets.add("password")
        await state.update_data(fields=fields, secrets=list(secrets))
        await _finalize_record(message, state)
        return
    if step == "sip_username":
        fields["sip_username"] = value
        await state.update_data(fields=fields, field_step="sip_password")
        await message.answer("Введите SIP Password:")
        return
    if step == "sip_password":
        fields["sip_password"] = value
        secrets.add("sip_password")
        await state.update_data(fields=fields, secrets=list(secrets))
        await _finalize_record(message, state)
        return

    await _finalize_record(message, state)


async def _finalize_record(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if not user:
            await message.answer("Ошибка авторизации")
            await state.clear()
            return
        try:
            record = await create_record(
                session,
                category_code=data["category"],
                record_type=RecordType(data["record_type"]),
                title=data["title"],
                description=data.get("description"),
                created_by=user,
                fields=data.get("fields") or {},
                secret_fields=set(data.get("secrets") or []),
            )
        except Exception as e:
            await message.answer(f"Ошибка сохранения: {e}")
            await state.clear()
            return

    await state.clear()
    await message.answer(f"✅ Запись «{record.title}» создана.")
