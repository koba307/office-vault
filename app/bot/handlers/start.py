import html

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.menu import get_main_menu_keyboard
from app.database.models import RoleCode, User, UserStatus
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active

router = Router()

ROLE_TITLES = {
    RoleCode.SUPERADMIN.value: "Суперадминистратор",
    RoleCode.BOSS.value: "Руководитель",
    RoleCode.EMPLOYEE.value: "Сотрудник",
}


def role_title(role: RoleCode | str | None) -> str:
    """Человекочитаемое название роли для приветствия."""
    value = getattr(role, "value", role)
    return ROLE_TITLES.get(str(value), "Сотрудник")


def invitation_text(user_id: int, name: str) -> str:
    """Сообщение для незарегистрированного пользователя: меню не показывается."""
    return (
        "🏢 <b>OFFICE VAULT</b>\n\n"
        f"Здравствуйте, {name}.\n"
        f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
        "Доступ предоставляется только зарегистрированным сотрудникам.\n"
        "Обратитесь к руководителю или суперадминистратору, чтобы вас добавили\n"
        "в систему и выдали права на рабочие разделы."
    )


def pending_text(name: str) -> str:
    return (
        "🏢 <b>OFFICE VAULT</b>\n\n"
        f"Здравствуйте, {name}.\n\n"
        "Ваша учётная запись создана и ожидает активации администратором.\n"
        "Повторите /start позже."
    )


def blocked_text(name: str) -> str:
    return (
        "🏢 <b>OFFICE VAULT</b>\n\n"
        f"Здравствуйте, {name}.\n\n"
        "Доступ к системе заблокирован.\n"
        "Обратитесь к суперадминистратору."
    )


def welcome_text(name: str, user: User) -> str:
    """Приветствие активного сотрудника: роль показывается по-человечески."""
    return (
        "🏢 <b>OFFICE VAULT</b>\n"
        "Корпоративное хранилище данных\n\n"
        f"Добро пожаловать, {name}! Выберите раздел для работы.\n"
        f"Роль: <b>{role_title(user.role)}</b>"
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    name = html.escape(message.from_user.full_name or "сотрудник")

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, user_id)

    if user is not None and await is_user_active(user):
        # Меню строится по роли и правам: EMPLOYEE / BOSS / SUPERADMIN
        kb = await get_main_menu_keyboard(user=user)
        await message.answer(welcome_text(name, user), reply_markup=kb)
        return

    if user is not None and user.status == UserStatus.PENDING:
        await message.answer(pending_text(name))
    elif user is not None:
        await message.answer(blocked_text(name))
    else:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Зарегистрировать", callback_data="reg:start")]]
        )
        await message.answer(invitation_text(user_id, name), reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🏢 <b>OFFICE VAULT — справка</b>\n\n"
        "• /start — главное меню\n"
        "• /cancel — отменить действие\n"
        "• /profile — мой профиль\n"
        "• /max_upload — загрузка MAX-аккаунтов (суперадмин)\n"
        "• /about — о боте\n\n"
        "Доступ к разделам назначается по роли.",
        parse_mode="HTML",
    )


@router.message(Command("about"))
async def cmd_about(message: Message) -> None:
    await message.answer(
        "🏢 <b>OFFICE VAULT</b>\n"
        "Корпоративное хранилище данных.\n\n"
        "Версия 1.0 · Дизайн-пакет v1.0 · Концепция «архитектура / тёплый минимализм».",
        parse_mode="HTML",
    )
