from uuid import UUID

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.backup.service import create_backup, list_backups, verify_backup
from app.services.permissions import PermissionCode

router = Router()


@router.callback_query(F.data == "backup:run")
async def run_backup(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user) or not user or user.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        backup = await create_backup(session)
    await callback.message.answer(
        f"✅ Бэкап создан: {backup.id}\n"
        f"Размер: {backup.size} байт\n"
        f"SHA-256: {backup.sha256}"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("backup:verify:"))
async def verify_backup_cb(callback: CallbackQuery) -> None:
    bid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user) or not user or user.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        ok = await verify_backup(session, bid)
    await callback.answer("Контрольная сумма совпадает" if ok else "Ошибка проверки", show_alert=True)
