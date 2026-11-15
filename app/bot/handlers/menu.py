from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import Category, RoleCode
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id
from app.services.permissions import PermissionService

router = Router()


async def get_main_menu_keyboard(user=None, is_admin: bool = False) -> InlineKeyboardMarkup:
    from sqlalchemy import select

    buttons = []

    # Показываем только доступные категории
    if user is not None:
        async with async_session() as session:
            result = await session.execute(
                select(Category).where(Category.status == "ACTIVE", Category.parent_id.is_(None))
            )
            categories = result.scalars().all()
            for cat in categories:
                if await PermissionService.can(user, "VIEW", "category", cat.id, session):
                    emoji = {"holodka": "❄️", "second_stage": "2️⃣", "closers": "🎯", "general": "🌐"}.get(
                        cat.code, "📁"
                    )
                    buttons.append(
                        [InlineKeyboardButton(text=f"{emoji} {cat.name}", callback_data=f"menu:category:{cat.code}")]
                    )

    buttons.append([InlineKeyboardButton(text="🔎 Поиск", callback_data="search:start")])
    buttons.append([InlineKeyboardButton(text="⭐ Избранное", callback_data="favorites:list")])
    buttons.append([InlineKeyboardButton(text="🕘 Недавнее", callback_data="recent:list")])
    buttons.append([InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile:view")])

    if is_admin:
        buttons.append([InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")])
        buttons.append([InlineKeyboardButton(text="🛡 Роли и доступы", callback_data="admin:roles")])
        buttons.append([InlineKeyboardButton(text="📋 Полный аудит", callback_data="admin:audit")])
        buttons.append([InlineKeyboardButton(text="💾 Резервные копии", callback_data="admin:backup")])
        buttons.append([InlineKeyboardButton(text="⚙️ Настройки системы", callback_data="admin:settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery) -> None:
    data = callback.data
    if data == "menu:main":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, callback.from_user.id)
            is_admin = bool(user and user.role == RoleCode.SUPERADMIN)
        await callback.message.edit_text(
            "🏢 <b>OFFICE VAULT</b>\n\nВыберите рабочий раздел:",
            reply_markup=await get_main_menu_keyboard(user=user, is_admin=is_admin),
        )
    elif data.startswith("menu:category:"):
        category = data.split(":")[-1]
        names = {
            "holodka": "❄️ ХОЛОДКА",
            "second_stage": "2️⃣ 2Й ЭТАП",
            "closers": "🎯 КЛОУЗЕРЫ",
            "general": "🌐 ОБЩЕЕ",
        }
        title = names.get(category, category)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔐 Логины и пароли", callback_data=f"type:credentials:{category}")],
                [InlineKeyboardButton(text="☎️ SIP и телефония", callback_data=f"type:sip:{category}")],
                [InlineKeyboardButton(text="🔗 Ссылки", callback_data=f"type:links:{category}")],
                [InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"type:settings:{category}")],
                [InlineKeyboardButton(text="📚 Инструкции", callback_data=f"type:instructions:{category}")],
                [InlineKeyboardButton(text="📁 Файлы и документы", callback_data=f"type:files:{category}")],
                [InlineKeyboardButton(text="🗂 Прочее", callback_data=f"type:other:{category}")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
            ]
        )
        await callback.message.edit_text(f"{title}\n\nВыберите тип информации:", reply_markup=kb)
    await callback.answer()
