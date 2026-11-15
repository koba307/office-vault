from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.database.models import RoleCode
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.files.service import upload_file
from app.services.import_export.service import export_records_csv

router = Router()


class UploadFile(StatesGroup):
    waiting_file = State()
    waiting_category = State()


class ImportCSV(StatesGroup):
    waiting_file = State()
    waiting_category = State()


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
    await message.answer("Введите код категории (holodka / second_stage / closers / general):")


@router.message(UploadFile.waiting_category)
async def finish_upload(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip()
    data = await state.get_data()
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if not is_user_active(user):
            await message.answer("Нет доступа")
            await state.clear()
            return
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
            await message.answer(f"✅ Файл «{record.title}» загружен.")
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
        if not is_user_active(user):
            await message.answer("Нет доступа")
            await state.clear()
            return
        if user.role not in (RoleCode.SUPERADMIN, RoleCode.BOSS):
            await message.answer("Нет прав")
            await state.clear()
            return
        from app.services.import_export.service import import_records_csv

        try:
            count = await import_records_csv(session, category_code=category, csv_text=data["csv_text"], created_by=user)
            await message.answer(f"✅ Импортировано записей: {count}")
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Ошибка импорта: {e}")
    await state.clear()


@router.callback_query(F.data == "export:csv")
async def do_export(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        csv_data = await export_records_csv(session)
    from aiogram.types import BufferedInputFile
    await callback.message.answer_document(
        BufferedInputFile(csv_data.encode(), filename="office_vault_export.csv"),
        caption="Экспорт записей",
    )
    await callback.answer()
