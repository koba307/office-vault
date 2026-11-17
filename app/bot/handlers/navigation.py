from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.menu import MAIN_TEXT, STALE_TEXT, build_main_menu, open_screen, render_main

# Основной роутер подключается до обработчиков разделов: /cancel должен работать внутри FSM-сценариев
router = Router()
# Ловушка для устаревших callback_data подключается последней
stale_router = Router()


@router.callback_query(F.data == "menu:main")
async def main_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    GlobalNavigation.clear(callback.from_user.id, chat_id)
    await render_main(callback)


@router.callback_query(F.data.startswith("menu:back"))
async def back_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    data = callback.data or ""
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    explicit = data.removeprefix("menu:back:") if data.startswith("menu:back:") else ""
    target = explicit or GlobalNavigation.back(callback.from_user.id, chat_id) or "menu:main"
    if not await open_screen(callback, target):
        await render_main(callback)


@router.callback_query(F.data == "menu:cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    GlobalNavigation.clear(callback.from_user.id, chat_id)
    await render_main(callback)
    await callback.answer("Действие отменено")


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    text = "Действие отменено." if had_state else "Незавершённых действий нет."
    kb = await build_main_menu(message.from_user.id)
    await message.answer(f"{text}\n\n{MAIN_TEXT}", reply_markup=kb)


@stale_router.callback_query(F.data)
async def stale_callback(callback: CallbackQuery) -> None:
    await callback.answer(STALE_TEXT, show_alert=True)


from app.bot.system.navigation import GlobalNavigation, NavigationManager


@router.callback_query(F.data == "sys:nav:back")
async def system_nav_back(callback: CallbackQuery, state: FSMContext) -> None:
    await NavigationManager.go_back(state)
    await callback.answer("Назад")
