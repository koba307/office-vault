from uuid import UUID

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import Role, User
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id

router = Router()


@router.callback_query(F.data.startswith("rbac:user:"))
async def rbac_user_roles(callback: CallbackQuery) -> None:
    uid = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        user = await session.get(User, UUID(uid))
        if user is None:
            await callback.answer("Не найден", show_alert=True)
            return
        roles = (await session.execute(__import__("sqlalchemy").select(Role))).scalars().all()
        from app.database.models import UserRole
        assigned = {
            r.role_id
            for r in (await session.execute(__import__("sqlalchemy").select(UserRole).where(UserRole.user_id == user.id))).scalars().all()
        }
    buttons = []
    for role in roles:
        mark = "✅" if role.id in assigned else "⬜"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {role.name}",
                    callback_data=f"rbac:toggleuser:{uid}:{role.id}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:user:{uid}")])
    await callback.message.edit_text(
        f"Роли данных для {user.display_name or user.telegram_id}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rbac:toggleuser:"))
async def rbac_toggle_user(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    uid, rid = parts[2], parts[3]
    from uuid import UUID as _UUID

    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        from sqlalchemy import delete, select

        from app.database.models import UserRole

        existing = await session.execute(
            select(UserRole).where(UserRole.user_id == _UUID(uid), UserRole.role_id == _UUID(rid))
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(UserRole(user_id=_UUID(uid), role_id=_UUID(rid)))
        else:
            await session.execute(
                delete(UserRole).where(UserRole.user_id == _UUID(uid), UserRole.role_id == _UUID(rid))
            )
        await session.commit()
    callback.data = f"rbac:user:{uid}"
    await rbac_user_roles(callback)


@router.callback_query(F.data.startswith("rbac:category:"))
async def rbac_category_roles(callback: CallbackQuery) -> None:
    code = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        from app.services.categories import get_category

        category = await get_category(session, code)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        roles = (await session.execute(__import__("sqlalchemy").select(Role))).scalars().all()
        from app.database.models import CategoryRole
        assigned = {
            r.role_id
            for r in (
                await session.execute(
                    __import__("sqlalchemy").select(CategoryRole).where(CategoryRole.category_id == category.id)
                )
            ).scalars().all()
        }
    buttons = []
    for role in roles:
        mark = "✅" if role.id in assigned else "⬜"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {role.name}",
                    callback_data=f"rbac:togglecategory:{code}:{role.id}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="category:manage")])
    await callback.message.edit_text(
        f"Роли для категории {category.name}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rbac:togglecategory:"))
async def rbac_toggle_category(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    code, rid = parts[2], parts[3]
    async with async_session() as session:
        me = await get_user_by_telegram_id(session, callback.from_user.id)
        if me is None or me.role.value != "SUPERADMIN":
            await callback.answer("Нет доступа", show_alert=True)
            return
        from app.services.categories import get_category

        category = await get_category(session, code)
        if category is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return
        from uuid import UUID as _UUID

        from sqlalchemy import delete, select

        from app.database.models import CategoryRole

        existing = await session.execute(
            select(CategoryRole).where(
                CategoryRole.category_id == category.id, CategoryRole.role_id == _UUID(rid)
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(CategoryRole(category_id=category.id, role_id=_UUID(rid)))
        else:
            await session.execute(
                delete(CategoryRole).where(
                    CategoryRole.category_id == category.id, CategoryRole.role_id == _UUID(rid)
                )
            )
        await session.commit()
    callback.data = f"rbac:category:{code}"
    await rbac_category_roles(callback)
