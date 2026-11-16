import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.layout import smart_layout
from app.database.models import CategoryRole, Role, User, UserRole
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.categories import get_category

router = Router()
logger = logging.getLogger(__name__)


def _markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))


@router.callback_query(F.data.startswith("rbac:u:") | F.data.startswith("rbac:user:"))
async def rbac_user_roles(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    raw = callback.data or ""
    if raw.startswith("rbac:user:"):
        uid = raw.removeprefix("rbac:user:")
    else:
        uid = raw.removeprefix("rbac:u:")
    try:
        from uuid import UUID as _UUID

        user_uuid = _UUID(uid)
    except (ValueError, TypeError):
        await callback.answer("Некорректный пользователь", show_alert=True)
        return
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        user = await session.get(User, user_uuid)
        if user is None:
            await callback.answer("Не найден", show_alert=True)
            return
        roles = (await session.execute(__import__("sqlalchemy").select(Role))).scalars().all()
        assigned = {
            r.role_id
            for r in (
                await session.execute(
                    __import__("sqlalchemy").select(UserRole).where(UserRole.user_id == user.id)
                )
            ).scalars().all()
        }
    await state.update_data(rbac_user_id=uid)
    buttons = []
    for role in roles:
        mark = "✅" if role.id in assigned else "⬜"
        buttons.append(
            [InlineKeyboardButton(text=f"{mark} {role.name}", callback_data=f"rbac:tu:{role.code}")]
        )
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:user:{uid}")])
    try:
        await callback.message.edit_text(
            f"Роли данных для {user.display_name or user.telegram_id}:",
            reply_markup=_markup(buttons),
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("rbac user edit failed")
        await callback.message.answer(f"Ошибка: {e}")
    await callback.answer()


@router.callback_query(F.data.startswith("rbac:tu:"))
async def rbac_toggle_user(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    code = (callback.data or "").removeprefix("rbac:tu:")
    data = await state.get_data()
    uid = data.get("rbac_user_id")
    if not uid:
        await callback.answer("Состояние устарело. Откройте заново.", show_alert=True)
        return
    from uuid import UUID as _UUID

    try:
        user_uuid = _UUID(uid)
    except (ValueError, TypeError):
        await callback.answer("Некорректный пользователь", show_alert=True)
        return
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        role = (await session.execute(__import__("sqlalchemy").select(Role).where(Role.code == code))).scalar_one_or_none()
        if role is None:
            await callback.answer("Роль не найдена", show_alert=True)
            return
        from sqlalchemy import delete, select

        existing = await session.execute(
            select(UserRole).where(UserRole.user_id == user_uuid, UserRole.role_id == role.id)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(UserRole(user_id=user_uuid, role_id=role.id))
        else:
            await session.execute(
                delete(UserRole).where(UserRole.user_id == user_uuid, UserRole.role_id == role.id)
            )
        await session.commit()
    # обновить экран
    callback.data = f"rbac:u:{uid}"
    await rbac_user_roles(callback, state)


@router.callback_query(F.data.startswith("rbac:c:") | F.data.startswith("rbac:category:"))
async def rbac_category_roles(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    raw = callback.data or ""
    if raw.startswith("rbac:category:"):
        code = raw.removeprefix("rbac:category:")
    else:
        code = raw.removeprefix("rbac:c:")
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        category = await get_category(session, code)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        roles = (await session.execute(__import__("sqlalchemy").select(Role))).scalars().all()
        assigned = {
            r.role_id
            for r in (
                await session.execute(
                    __import__("sqlalchemy").select(CategoryRole).where(CategoryRole.category_id == category.id)
                )
            ).scalars().all()
        }
    await state.update_data(rbac_category_code=code)
    buttons = []
    for role in roles:
        mark = "✅" if role.id in assigned else "⬜"
        buttons.append(
            [InlineKeyboardButton(text=f"{mark} {role.name}", callback_data=f"rbac:tc:{role.code}")]
        )
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="category:manage")])
    try:
        await callback.message.edit_text(
            f"Роли для категории {category.name}:",
            reply_markup=_markup(buttons),
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("rbac category edit failed")
        await callback.message.answer(f"Ошибка: {e}")
    await callback.answer()


@router.callback_query(F.data.startswith("rbac:tc:"))
async def rbac_toggle_category(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    code = (callback.data or "").removeprefix("rbac:tc:")
    data = await state.get_data()
    cat_code = data.get("rbac_category_code") or code
    # code здесь — код роли, категория берётся из state
    category_code = data.get("rbac_category_code")
    if not category_code:
        await callback.answer("Состояние устарело. Откройте заново.", show_alert=True)
        return
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        category = await get_category(session, category_code)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        role = (await session.execute(__import__("sqlalchemy").select(Role).where(Role.code == code))).scalar_one_or_none()
        if role is None:
            await callback.answer("Роль не найдена", show_alert=True)
            return
        from sqlalchemy import delete, select

        existing = await session.execute(
            select(CategoryRole).where(
                CategoryRole.category_id == category.id, CategoryRole.role_id == role.id
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(CategoryRole(category_id=category.id, role_id=role.id))
        else:
            await session.execute(
                delete(CategoryRole).where(
                    CategoryRole.category_id == category.id, CategoryRole.role_id == role.id
                )
            )
        await session.commit()
    callback.data = f"rbac:c:{category_code}"
    await rbac_category_roles(callback, state)
