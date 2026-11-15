from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.menu import SORT_DATE, render_records_list
from app.bot.states.add_record import AddRecord, EditRecord
from app.core.encryption import decrypt_secret
from app.database.models import RecordType, RoleCode
from app.database.session import async_session
from app.services.auth.permissions import (
    get_user_by_telegram_id,
    is_user_active,
    log_audit,
)
from app.services.records.service import (
    archive_record,
    create_record,
    get_record,
    soft_delete_record,
    update_record,
)

router = Router()


@router.callback_query(F.data.startswith("type:"))
async def type_callback(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Некорректный запрос")
        return

    rec_type = parts[1].upper()
    category = parts[2]

    try:
        RecordType(rec_type)
    except ValueError:
        await callback.answer("Неизвестный тип")
        return

    await render_records_list(callback, category, rec_type, SORT_DATE, 0)


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
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Ошибка сохранения: {e}")
            await state.clear()
            return

    await state.clear()
    await message.answer(f"✅ Запись «{record.title}» создана.")


@router.callback_query(F.data.startswith("record:view:"))
async def view_record(callback: CallbackQuery) -> None:
    rid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from uuid import UUID
        record = await get_record(session, UUID(rid))
        if not record:
            await callback.answer("Запись не найдена")
            return

    lines = [f"📄 <b>{record.title}</b>\n"]
    if record.description:
        lines.append(record.description + "\n")
    buttons = []
    for f in sorted(record.fields, key=lambda x: x.sort_order):
        if f.encrypted_value:
            lines.append(f"{f.field_label}: ••••••••")
            buttons.append([InlineKeyboardButton(text=f"👁 Показать {f.field_label}", callback_data=f"record:reveal:{record.id}:{f.id}")])
        else:
            lines.append(f"{f.field_label}: {f.value_text or '-'}")
    buttons.append([InlineKeyboardButton(text="⭐ В избранное", callback_data=f"favorite:add:{record.id}")])
    if record.record_type.value == "FILES":
        buttons.append([InlineKeyboardButton(text="⬇️ Скачать файл", callback_data=f"record:download:{record.id}")])
    if user and user.role in (RoleCode.SUPERADMIN, RoleCode.BOSS):
        buttons.append([InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"record:edit:{record.id}")])
        buttons.append([InlineKeyboardButton(text="🗄 Архивировать", callback_data=f"menu:ask:archive:{record.id}")])
        buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"menu:ask:delete:{record.id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])

    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("record:download:"))
async def download_file(callback: CallbackQuery) -> None:
    rid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from uuid import UUID

        record = await get_record(session, UUID(rid))
        if not record:
            await callback.answer("Запись не найдена")
            return
        field_map = {f.field_key: f.value_text for f in record.fields}
        # encrypted? no
        bucket = field_map.get("bucket") or "office-vault"
        object_name = field_map.get("object_name")
        filename = field_map.get("filename") or record.title
        if not object_name:
            await callback.answer("Файл не найден")
            return

    from app.services.files.service import get_minio_client

    client = get_minio_client()
    try:
        response = client.get_object(bucket, object_name)
        data = response.read()
        response.close()
        response.release_conn()
    except Exception as e:  # noqa: BLE001
        await callback.answer(f"Ошибка скачивания: {e}", show_alert=True)
        return

    from aiogram.types import BufferedInputFile

    await callback.message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=f"Файл: {record.title}",
    )
    await callback.answer("Файл отправлен")


@router.callback_query(F.data.startswith("record:reveal:"))
async def reveal_secret(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    rid, fid = parts[2], parts[3]
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        from uuid import UUID
        record = await get_record(session, UUID(rid))
        if not record:
            await callback.answer("Не найдено")
            return
        field = next((f for f in record.fields if str(f.id) == fid), None)
        if not field or not field.encrypted_value:
            await callback.answer("Нет секрета")
            return
        plain = decrypt_secret(field.encrypted_value)
        await log_audit(session, user.id, "REVEAL_SECRET", "RecordField", fid)

    await callback.message.answer(f"🔑 {field.field_label}:\n<code>{plain}</code>", parse_mode="HTML")
    await callback.answer("Показано. Секрет будет в истории чата.")


@router.callback_query(F.data.startswith("record:edit:"))
async def start_edit_record(callback: CallbackQuery, state: FSMContext) -> None:
    rid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not user or user.role not in (RoleCode.SUPERADMIN, RoleCode.BOSS):
            await callback.answer("Нет прав", show_alert=True)
            return
        from uuid import UUID

        record = await get_record(session, UUID(rid))
        if not record:
            await callback.answer("Не найдено")
            return
    await state.update_data(record_id=rid)
    await state.set_state(EditRecord.waiting_title)
    await callback.message.answer(f"Текущее название: {record.title}\nВведите новое название (или «-» чтобы оставить):")
    await callback.answer()


@router.message(EditRecord.waiting_title)
async def edit_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if title != "-":
        await state.update_data(title=title)
    else:
        await state.update_data(title=None)
    await state.set_state(EditRecord.waiting_description)
    await message.answer("Введите новое описание (или «-» чтобы оставить, «--» чтобы очистить):")


@router.message(EditRecord.waiting_description)
async def edit_description(message: Message, state: FSMContext) -> None:
    desc_raw = (message.text or "").strip()
    data = await state.get_data()
    from uuid import UUID

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if not user:
            await message.answer("Ошибка авторизации")
            await state.clear()
            return
        title = data.get("title")
        if desc_raw == "-":
            description = None  # keep? we pass None meaning no change? Need distinguish.
        elif desc_raw == "--":
            description = ""
        else:
            description = desc_raw

        # We'll treat None as no change; empty string as clear. But update_record accepts None as no change.
        # To allow clear, pass empty string.
        await update_record(
            session,
            UUID(data["record_id"]),
            title=title,
            description=description if description != "" else "",
            user_id=user.id,
        )
    await state.clear()
    await message.answer("✅ Запись обновлена.")


@router.callback_query(F.data.startswith("record:archive:"))
async def do_archive(callback: CallbackQuery) -> None:
    rid = (callback.data or "").split(":")[-1]
    from uuid import UUID

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not user or user.role not in (RoleCode.SUPERADMIN, RoleCode.BOSS):
            await callback.answer("Нет прав", show_alert=True)
            return
        await archive_record(session, UUID(rid), user.id)
    await callback.answer("Архивировано")
    await callback.message.edit_text("Запись перемещена в архив.")


@router.callback_query(F.data.startswith("record:delete:"))
async def do_delete(callback: CallbackQuery) -> None:
    rid = (callback.data or "").split(":")[-1]
    from uuid import UUID

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not user or user.role not in (RoleCode.SUPERADMIN, RoleCode.BOSS):
            await callback.answer("Нет прав", show_alert=True)
            return
        await soft_delete_record(session, UUID(rid), user.id)
    await callback.answer("Удалено (мягко)")
    await callback.message.edit_text("Запись помечена как удалённая.")
