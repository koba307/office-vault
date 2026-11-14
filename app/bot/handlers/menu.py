from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import RoleCode
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id

router = Router()


def get_main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="❄️ Холодка", callback_data="menu:category:holodka")],
        [InlineKeyboardButton(text="2️⃣ 2й Этап", callback_data="menu:category:second_stage")],
        [InlineKeyboardButton(text="🎯 Клоузеры", callback_data="menu:category:closers")],
        [InlineKeyboardButton(text="🌐 Общее", callback_data="menu:category:general")],
        [InlineKeyboardButton(text="🔎 Поиск", callback_data="search:start")],
        [InlineKeyboardButton(text="⭐ Избранное", callback_data="favorites:list")],
        [InlineKeyboardButton(text="🕘 Недавнее", callback_data="recent:list")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile:view")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")])
        buttons.append([InlineKeyboardButton(text="🛡 Роли и доступы", callback_data="admin:roles")])
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
            reply_markup=get_main_menu_keyboard(is_admin=is_admin),
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
