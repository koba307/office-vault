import html
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.keyboards.layout import smart_layout
from app.bot.keyboards.pagination import confirm_keyboard
from app.database.models import File, PermissionCode, RoleCode, ScanStatus, User
from app.database.session import async_session
from app.services.auth.permissions import (
    get_user_by_telegram_id,
    is_user_active,
    log_audit,
)
from app.services.files.access import (
    ACCESS_SUCCESS,
    check_file_access,
    check_file_download,
    log_file_access,
)
from app.services.files.service import (
    FileError,
    FileInfectedError,
    FileSecurityError,
    archive_file,
    download_file_bytes,
    restore_file,
    soft_delete_file,
    upload_file,
)
from app.services.files.versions import (
    FileVersionError,
    current_version_number,
    list_file_versions,
    replace_file,
    restore_file_version,
)
from app.services.permissions import PermissionDenied, PermissionService
from app.services.permissions.service import DENY_MESSAGES, TARGET_FILE

router = Router()

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_VERSION_BUTTONS = 10

SCAN_LABELS: dict[ScanStatus, str] = {
    ScanStatus.CLEAN: "чисто",
    ScanStatus.PENDING: "ожидает проверки",
    ScanStatus.INFECTED: "заражён",
    ScanStatus.ERROR: "ошибка проверки",
    ScanStatus.SKIPPED: "проверка не выполнялась",
}


class UploadFile(StatesGroup):
    waiting_file = State()
    waiting_category = State()


class ImportCSV(StatesGroup):
    waiting_file = State()
    waiting_category = State()


class ReplaceFile(StatesGroup):
    waiting_document = State()


def _parse_uuid(value: str | None) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _escape(value) -> str:
    return html.escape(str(value), quote=False)


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024**2:
        return f"{size / 1024:.1f} КБ"
    return f"{size / 1024**2:.1f} МБ"


def _author_name(user: User | None) -> str:
    if user is None:
        return "неизвестно"
    if user.display_name:
        return user.display_name
    if user.username:
        return f"@{user.username}"
    return str(user.telegram_id)


async def _active_user(session, telegram_id: int) -> User | None:
    user = await get_user_by_telegram_id(session, telegram_id)
    return user if await is_user_active(user) else None


async def _deny_text(
    user: User | None, action: PermissionCode, file_id: UUID, session
) -> str | None:
    """Текст отказа или None, если действие разрешено."""
    try:
        await PermissionService.require(user, action, TARGET_FILE, file_id, session)
    except PermissionDenied as exc:
        return DENY_MESSAGES.get(exc.reason, "Нет доступа")
    return None


async def _file_card(session, file: File, user: User) -> tuple[str, InlineKeyboardMarkup]:
    author = await session.get(User, file.uploaded_by)
    version = await current_version_number(file.id, session)
    text = (
        f"📄 <b>{_escape(file.original_name)}</b>\n"
        f"Размер: {_format_size(file.size)}\n"
        f"MIME: {_escape(file.mime_type)}\n"
        f"SHA-256: <code>{_escape(file.sha256 or '—')}</code>\n"
        f"Загружен: {file.created_at:%d.%m.%Y %H:%M} UTC\n"
        f"Автор: {_escape(_author_name(author))}\n"
        f"Версия: {version}\n"
        f"Проверка: {SCAN_LABELS.get(file.scan_status, _escape(file.scan_status))}"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if file.deleted_at is not None:
        text += "\nСтатус: 🛑 удалён (корзина)"
        if await PermissionService.can(
            user, PermissionCode.RESTORE, TARGET_FILE, file.id, session
        ):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="♻️ Восстановить", callback_data=f"files:undelete:{file.id}"
                    )
                ]
            )
        return text, InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))

    can_download = await check_file_download(user, file.id, session)
    can_edit = await PermissionService.can(user, PermissionCode.EDIT, TARGET_FILE, file.id, session)
    if can_download:
        rows.append(
            [InlineKeyboardButton(text="⬇️ Скачать", callback_data=f"files:download:{file.id}")]
        )
    rows.append([InlineKeyboardButton(text="🕘 Версии", callback_data=f"files:versions:{file.id}")])
    if can_edit:
        rows.append(
            [InlineKeyboardButton(text="🔁 Заменить", callback_data=f"files:replace:{file.id}")]
        )
    if await PermissionService.can(user, PermissionCode.ARCHIVE, TARGET_FILE, file.id, session):
        rows.append(
            [InlineKeyboardButton(text="🗄 Архивировать", callback_data=f"files:archive:{file.id}")]
        )
    if await PermissionService.can(user, PermissionCode.DELETE, TARGET_FILE, file.id, session):
        rows.append(
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"files:delete:{file.id}")]
        )
    return text, InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))


def _upload_metadata_text(file: File) -> str:
    """Метаданные файла сразу после загрузки: MIME, SHA-256 и статус скана."""
    text = (
        f"📄 <b>{_escape(file.original_name)}</b>\n"
        f"MIME: <code>{_escape(file.mime_type)}</code>\n"
        f"Размер: {_format_size(file.size)}\n"
        f"SHA-256: <code>{_escape(file.sha256 or '—')}</code>\n"
        f"Проверка: {SCAN_LABELS.get(file.scan_status, _escape(file.scan_status))}"
    )
    if file.scan_status != ScanStatus.CLEAN:
        text += "\nФайл в карантине: скачивание недоступно до успешной проверки."
    return text


def _uploaded_file_id(record) -> UUID | None:
    for field in getattr(record, "fields", []) or []:
        if field.field_key == "file_id" and field.value_text:
            return _parse_uuid(field.value_text)
    return None


@router.message(Command("file"))
async def show_file(message: Message, command: CommandObject) -> None:
    """Карточка файла с метаданными: /file <id файла>."""
    file_id = _parse_uuid(command.args)
    if file_id is None:
        await message.answer("Использование: /file <id файла>")
        return
    async with async_session() as session:
        user = await _active_user(session, message.from_user.id)
        if user is None:
            await message.answer("Нет доступа")
            return
        if not await check_file_access(user, file_id, session):
            await message.answer("Файл не найден или недоступен.")
            return
        file = await session.get(File, file_id)
        text, markup = await _file_card(session, file, user)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("files:download:"))
async def download_file(callback: CallbackQuery) -> None:
    file_id = _parse_uuid((callback.data or "").split(":")[-1])
    if file_id is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            file, data = await download_file_bytes(session, user=user, file_id=file_id)
        except PermissionDenied as exc:
            await callback.answer(DENY_MESSAGES.get(exc.reason, "Нет доступа"), show_alert=True)
            return
        except Exception as e:  # noqa: BLE001 — хранилище недоступно
            await callback.answer(f"Ошибка скачивания: {e}", show_alert=True)
            return

    await callback.message.answer_document(
        BufferedInputFile(data, filename=file.original_name),
        caption=f"📎 {file.original_name}",
    )
    await callback.answer("Файл отправлен")


@router.callback_query(F.data.startswith("files:card:"))
async def show_file_card(callback: CallbackQuery) -> None:
    """Карточка файла по callback (возврат после действий и подтверждений)."""
    file_id = _parse_uuid((callback.data or "").split(":")[-1])
    if file_id is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await check_file_access(user, file_id, session):
            await callback.answer("Файл не найден или недоступен", show_alert=True)
            return
        file = await session.get(File, file_id)
        text, markup = await _file_card(session, file, user)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("files:archive:"))
async def archive_file_handler(callback: CallbackQuery) -> None:
    """Архивирование файла (soft): скрыть из рабочих экранов, данные остаются."""
    file_id = _parse_uuid((callback.data or "").split(":")[-1])
    if file_id is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            file = await archive_file(session, user=user, file_id=file_id)
        except PermissionDenied as exc:
            await callback.answer(DENY_MESSAGES.get(exc.reason, "Нет доступа"), show_alert=True)
            return
        except FileError as e:
            await callback.answer(str(e), show_alert=True)
            return
        await log_audit(session, user.id, "ARCHIVE_FILE", "File", str(file_id))
        text, markup = await _file_card(session, file, user)
    await callback.answer("Файл архивирован")
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("files:delete:"))
async def delete_file_ask(callback: CallbackQuery) -> None:
    """Удаление файла только после подтверждения (ТЗ Phase 06)."""
    file_id = _parse_uuid((callback.data or "").split(":")[-1])
    if file_id is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        deny = await _deny_text(user, PermissionCode.DELETE, file_id, session)
        if deny is not None:
            await callback.answer(deny, show_alert=True)
            return
        file = await session.get(File, file_id)
        if file is None:
            await callback.answer("Файл не найден", show_alert=True)
            return
        name = _escape(file.original_name)
    await callback.message.edit_text(
        f"🗑 <b>Удалить файл «{name}» в корзину?</b>\n\n"
        "Скачивание будет остановлено. Восстановление доступно "
        "владельцу и администраторам из корзины.",
        reply_markup=confirm_keyboard(f"files:delete_yes:{file_id}", f"files:card:{file_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("files:delete_yes:"))
async def delete_file_confirm(callback: CallbackQuery) -> None:
    """Soft delete файла после подтверждения: deleted_at + журнал + аудит."""
    file_id = _parse_uuid((callback.data or "").split(":")[-1])
    if file_id is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            file = await soft_delete_file(session, user=user, file_id=file_id)
        except PermissionDenied as exc:
            await callback.answer(DENY_MESSAGES.get(exc.reason, "Нет доступа"), show_alert=True)
            return
        except FileError as e:
            await callback.answer(str(e), show_alert=True)
            return
        await log_audit(session, user.id, "DELETE_FILE", "File", str(file_id))
        text, markup = await _file_card(session, file, user)
    await callback.answer("Файл удалён в корзину")
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("files:undelete:"))
async def undelete_file(callback: CallbackQuery) -> None:
    """Восстановление файла из корзины/архива."""
    file_id = _parse_uuid((callback.data or "").split(":")[-1])
    if file_id is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            file = await restore_file(session, user=user, file_id=file_id)
        except PermissionDenied as exc:
            await callback.answer(DENY_MESSAGES.get(exc.reason, "Нет доступа"), show_alert=True)
            return
        except FileError as e:
            await callback.answer(str(e), show_alert=True)
            return
        await log_audit(session, user.id, "RESTORE_FILE", "File", str(file_id))
        text, markup = await _file_card(session, file, user)
    await callback.answer("Файл восстановлен")
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("files:versions:"))
async def versions_screen(callback: CallbackQuery) -> None:
    file_id = _parse_uuid((callback.data or "").split(":")[-1])
    if file_id is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None or not await check_file_access(user, file_id, session):
            await callback.answer("Нет доступа", show_alert=True)
            return
        file = await session.get(File, file_id)
        versions = await list_file_versions(file_id, session)
        current = await current_version_number(file_id, session)
        can_edit = await PermissionService.can(
            user, PermissionCode.EDIT, TARGET_FILE, file_id, session
        )

    lines = [f"🕘 <b>{_escape(file.original_name)}</b>", f"Текущая версия: {current}"]
    for item in versions:
        lines.append(
            f"v{item.version} · {_format_size(item.size)} · "
            f"{item.created_at:%d.%m.%Y %H:%M} · <code>{_escape((item.sha256 or '—')[:12])}</code>"
        )
    if not versions:
        lines.append("Сохранённых версий пока нет.")

    rows: list[list[InlineKeyboardButton]] = []
    if can_edit:
        for item in versions[:MAX_VERSION_BUTTONS]:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"↩️ Вернуть версию {item.version}",
                        callback_data=f"files:restore:{file_id}:{item.version}",
                    )
                ]
            )
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=smart_layout(rows)) if rows else None,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("files:restore:"))
async def restore_version(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    file_id = _parse_uuid(parts[2]) if len(parts) == 4 else None
    if file_id is None or not parts[3].isdigit():
        await callback.answer("Некорректная версия")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        deny = await _deny_text(user, PermissionCode.EDIT, file_id, session)
        if deny is not None:
            await callback.answer(deny, show_alert=True)
            return
        try:
            file = await restore_file_version(file_id, int(parts[3]), session)
        except FileVersionError as e:
            await callback.answer(str(e), show_alert=True)
            return
        await log_file_access(file_id, user.id, "RESTORE", ACCESS_SUCCESS, session)
    await callback.message.answer(
        f"✅ Версия {parts[3]} восстановлена для «{_escape(file.original_name)}».",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("files:replace:"))
async def start_replace(callback: CallbackQuery, state: FSMContext) -> None:
    file_id = _parse_uuid((callback.data or "").split(":")[-1])
    if file_id is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        deny = await _deny_text(user, PermissionCode.EDIT, file_id, session)
        if deny is not None:
            await callback.answer(deny, show_alert=True)
            return

    await state.update_data(replace_file_id=str(file_id))
    await state.set_state(ReplaceFile.waiting_document)
    await callback.message.answer("Отправьте новую версию файла документом. Для отмены — /cancel.")
    await callback.answer()


@router.message(ReplaceFile.waiting_document, F.document)
async def receive_replacement(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = _parse_uuid(data.get("replace_file_id"))
    await state.clear()
    if file_id is None:
        await message.answer("Экран устарел — откройте файл заново.")
        return

    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.answer("Файл слишком большой (максимум 25 МБ).")
        return

    raw = (await message.bot.download(doc)).read()

    async with async_session() as session:
        user = await _active_user(session, message.from_user.id)
        if user is None:
            await message.answer("Нет доступа")
            return
        deny = await _deny_text(user, PermissionCode.EDIT, file_id, session)
        if deny is not None:
            await message.answer(deny)
            return
        try:
            file = await replace_file(file_id, raw, session)
        except ValueError as e:
            await message.answer(f"Ошибка: {e}")
            return
        except Exception as e:  # noqa: BLE001 — MinIO/сеть недоступны
            await message.answer(f"Не удалось сохранить файл: {e}")
            return
        version = await current_version_number(file_id, session)
        await log_file_access(file_id, user.id, "REPLACE", ACCESS_SUCCESS, session)

    await message.answer(
        f"✅ Файл «{_escape(file.original_name)}» заменён. Текущая версия: {version}.",
        parse_mode="HTML",
    )


@router.message(ReplaceFile.waiting_document)
async def replace_wrong_input(message: Message) -> None:
    await message.answer("Нужно отправить файл-документ. Для отмены — /cancel.")


@router.callback_query(F.data == "files:upload")
async def start_upload(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UploadFile.waiting_file)
    await callback.message.answer("Отправьте файл (документ).")
    await callback.answer()


@router.message(UploadFile.waiting_file, F.document)
async def receive_file(message: Message, state: FSMContext) -> None:
    doc = message.document
    file = await message.bot.download(doc)
    content = file.read()
    await state.update_data(file_bytes=content, filename=doc.file_name, content_type=doc.mime_type)
    await state.set_state(UploadFile.waiting_category)
    await message.answer(
        f"📥 Файл «{_escape(doc.file_name or 'document')}» получен.\n"
        "Введите код категории (holodka / second_stage / closers / general).\n\n"
        "После выбора категории файл пройдёт проверку MIME, SHA-256 и антивируса."
    )


@router.message(UploadFile.waiting_category)
async def finish_upload(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip()
    data = await state.get_data()
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if not await is_user_active(user):
            await message.answer("Нет доступа")
            await state.clear()
            return
        await message.answer("⏳ Проверяю файл: MIME, SHA-256 и антивирус…")
        try:
            record = await upload_file(
                session,
                category_code=category,
                title=data["filename"],
                description=None,
                file_bytes=data["file_bytes"],
                filename=data["filename"],
                content_type=data.get("content_type") or "application/octet-stream",
                created_by_id=user.id,
            )
            text = f"✅ Файл «{_escape(record.title)}» загружен."
            file_id = _uploaded_file_id(record)
            file = await session.get(File, file_id) if file_id else None
            if file is not None:
                text = f"{text}\n\n{_upload_metadata_text(file)}"
            await message.answer(text, parse_mode="HTML")
        except PermissionDenied as e:
            await message.answer(DENY_MESSAGES.get(e.reason, "Нет прав"))
        except FileInfectedError as e:
            await message.answer(f"⛔ {e}")
        except FileSecurityError as e:
            await message.answer(f"⛔ Файл отклонён проверкой безопасности: {e}")
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Ошибка: {e}")
    await state.clear()


@router.callback_query(F.data == "import:csv")
async def start_import(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImportCSV.waiting_file)
    await callback.message.answer("Отправьте CSV файл для импорта.")
    await callback.answer()


@router.message(ImportCSV.waiting_file, F.document)
async def receive_import_file(message: Message, state: FSMContext) -> None:
    doc = message.document
    file = await message.bot.download(doc)
    content = file.read().decode("utf-8", errors="replace")
    await state.update_data(csv_text=content)
    await state.set_state(ImportCSV.waiting_category)
    await message.answer("Введите код категории (holodka / second_stage / closers / general):")


@router.message(ImportCSV.waiting_category)
async def finish_import(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip()
    data = await state.get_data()
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if not await is_user_active(user):
            await message.answer("Нет доступа")
            await state.clear()
            return
        if user.role not in (RoleCode.SUPERADMIN, RoleCode.BOSS):
            await message.answer("Нет прав")
            await state.clear()
            return
        from app.services.import_export.service import import_records_csv

        try:
            count = await import_records_csv(
                session, category_code=category, csv_text=data["csv_text"], created_by=user
            )
            await message.answer(f"✅ Импортировано записей: {count}")
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Ошибка импорта: {e}")
    await state.clear()
