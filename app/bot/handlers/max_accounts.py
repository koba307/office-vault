from aiogram import F, Router
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
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.max_accounts.service import (
    confirm_issue,
    release_reservation,
    remaining_max_accounts,
    reserve_max_account,
    upload_max_accounts,
)

router = Router()


class MaxUpload(StatesGroup):
    waiting_file = State()


async def _user_active(session, telegram_id):
    user = await get_user_by_telegram_id(session, telegram_id)
    if not user or not await is_user_active(user):
        return None
    return user


def _markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))


@router.callback_query(F.data.in_({"max:menu", "category:view:max"}))
async def max_menu(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await _user_active(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        remaining = await remaining_max_accounts(session)

    rows = []
    if remaining > 0:
        text = f"📦 <b>MAX</b>\n\nДоступно аккаунтов: <b>{remaining}</b>"
        rows.append([InlineKeyboardButton(text="📥 Получить", callback_data="max:get")])
    else:
        text = "📦 <b>MAX</b>\n\nАккаунтов нет."
    if user.role.value == "SUPERADMIN":
        rows.append([InlineKeyboardButton(text="📤 Загрузить аккаунты", callback_data="max:upload")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back:menu:main")])
    await callback.message.edit_text(text, reply_markup=_markup(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "max:get")
async def max_get(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await _user_active(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        reserved = await reserve_max_account(session, user)
        if reserved is None:
            await callback.answer("Аккаунтов нет", show_alert=True)
            await callback.message.edit_text(
                "📦 <b>MAX</b>\n\nАккаунтов нет.",
                reply_markup=_markup([[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back:menu:main")]]),
                parse_mode="HTML",
            )
            return
        file, data = reserved
        try:
            await callback.message.answer_document(
                BufferedInputFile(data, filename=file.original_name),
                caption="MAX-аккаунт выдан вам.",
            )
        except Exception:  # noqa: BLE001 — файл не отправился, снимаем резерв
            await release_reservation(session, file.id)
            await callback.answer("Не удалось отправить аккаунт. Попробуйте ещё раз.", show_alert=True)
            return
        await confirm_issue(session, file.id)
        remaining = await remaining_max_accounts(session)
    await callback.answer("Аккаунт отправлен")
    await callback.message.edit_text(
        f"📦 <b>MAX</b>\n\nДоступно аккаунтов: <b>{remaining}</b>",
        reply_markup=_markup([[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back:menu:main")]]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "max:upload")
async def max_upload_start(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        user = await _user_active(session, callback.from_user.id)
        if user is None or user.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
    await state.set_state(MaxUpload.waiting_file)
    await callback.message.edit_text(
        "📤 Отправьте .txt файл или .zip архив с .txt файлами.\n"
        "Для отмены — /cancel.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(MaxUpload.waiting_file, F.document)
async def max_upload_file(message: Message, state: FSMContext) -> None:
    async with async_session() as session:
        user = await _user_active(session, message.from_user.id)
        if user is None or user.role.value != "SUPERADMIN":
            await message.answer("Нет доступа.")
            await state.clear()
            return
        document = message.document
        filename = (document.file_name or "accounts.txt").strip()
        if not (filename.lower().endswith(".txt") or filename.lower().endswith(".zip")):
            await message.answer("Нужен .txt или .zip с .txt файлами.")
            return
        file = await message.bot.get_file(document.file_id)
        data = await message.bot.download_file(file.file_path)
        try:
            count = await upload_max_accounts(session, user, bytes(data), filename)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await message.answer(f"✅ Добавлено MAX-аккаунтов: <b>{count}</b>", parse_mode="HTML")
